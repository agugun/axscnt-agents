"""History matching on Norne (phase 4 of docs/design/history-match.md; see testing/history-match-norne.md).

Norne (opm-tests, NORNE_ATW2013) as a study from its deck: imported into four models, approved, rebuilt and run;
then its misfit against the history its own deck gives Flow (WCONHIST / WCONINJH, 1997-2006), and a sensitivity on
PERMX per formation (layer groups) and the transmissibility of four faults. Reported, not a pass mark: the deck is
the benchmark's starting model.

    python testing/hm_norne.py --out <dir> [--norne <NORNE_ATW2013.DATA>] [--parallel 3] [--skip-sensitivity]

Writes <dir>/hm_norne.json (the numbers) and prints where the HM reports are. A rerun with the same --out reuses
the workspace: the import, the base run and every finished case.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from opmagent.core import db
from opmagent.core.config import Config

NORNE = Path.home() / ".local/share/opmagent/open-data/opm-tests/norne/NORNE_ATW2013.DATA"
# Norne's formations as layer groups of its 22 layers (Not, layer 4, is a shale: no PERMX to multiply)
GROUPS = {"Garn": "1-3", "Ile": "5-11", "Tofte": "12-14", "Tilje": "15-22"}
FAULTS = ["BC", "DI", "C_01", "E_01"]           # large faults with a transmissibility multiplier below 1
# measurement error for a field of Norne's size: rates 10 % with a floor of 20 sm3/d (gas 5000), BHP 10 bar
OBJECTIVE = {"sigma": {"oil": {"rel": 0.1, "abs": 20}, "water": {"rel": 0.1, "abs": 20},
                       "gas": {"rel": 0.1, "abs": 5000}, "water_inj": {"rel": 0.1, "abs": 50},
                       "gas_inj": {"rel": 0.1, "abs": 5000}, "bhp": {"rel": 0.0, "abs": 10}}}


def say(*a):
    print(*a, flush=True)


def workspace(out: Path, parallel: int) -> Config:
    cfg = Config(database=out / "opm.db", file_store=out / "files", qc_report="html", hm_parallel=parallel,
                 hm_threads=2, hm_case_memory_mb=1500, hm_min_free_mb=1024)
    if not cfg.database.exists():
        out.mkdir(parents=True, exist_ok=True)
        db.init_db(cfg.database)
        for s in ("inbox", "qc"):
            (cfg.file_store / s).mkdir(parents=True, exist_ok=True)
    return cfg


def base_deck_and_run(cfg, norne: Path) -> tuple[int, int]:
    """Import Norne, approve its four models, rebuild its deck, accept its initial state, run it (all reused)."""
    from opmagent.deck.build import build_deck
    from opmagent.deck.import_pipeline import _approved_import, import_to_drafts
    from opmagent.deck.init_review import accept, state
    from opmagent.servers.data_server import DataService
    from opmagent.servers.sim_server import SimService
    from opmagent.servers.sim_server import wrap as swrap
    rv = DataService("reviewer", cfg)
    con = rv.con
    deck = con.execute("SELECT id FROM decks WHERE name = 'NORNE_HM' AND status = 'validated'").fetchone()
    if not deck:
        if not _approved_import(cfg, "NORNE"):
            t = time.time()
            r = import_to_drafts(cfg, str(norne), "NORNE")
            if not r["ok"]:
                sys.exit(f"import failed: {r.get('spoken_summary')}")
            for p in rv.list_pending_reviews()["pending"]:
                rid = rv.get_qc_report(kind=p["kind"], version_id=p["version_id"])["qc_report"]["id"]
                rv.approve(kind=p["kind"], version_id=p["version_id"], qc_report_file_id=rid)
            say(f"imported and approved Norne's four models ({time.time() - t:.0f} s)")
        base, versions = _approved_import(cfg, "NORNE")
        spec = json.loads((base / "deck_spec.json").read_text())
        spec.update({k: int(versions[k]) for k in ("fluid", "geo", "res", "well")}, name="NORNE_HM")
        d = build_deck(spec, cfg, origin="built")
        if not d["ok"]:
            sys.exit(f"deck build failed: {d['spoken_summary']}")
        deck_id = d["deck_id"]
    else:
        deck_id = deck["id"]
    if state(con, deck_id) == "waiting":
        accept(cfg, con, deck_id, "hm_norne.py")
    run = con.execute("SELECT id FROM runs WHERE deck_id = ? AND status IN ('done','stored') ORDER BY id DESC",
                      (deck_id,)).fetchone()
    if run:
        return deck_id, run["id"]
    sim = SimService(cfg)
    a = swrap(sim, "run_simulation")(deck_id=deck_id)
    if not a.get("ok"):
        sys.exit(f"run failed to start: {a.get('message')}")
    t = time.time()
    say(f"running Norne (run {a['run_id']}); about 5 min")
    w = swrap(sim, "wait")(run_id=a["run_id"], timeout_s=7200)
    if not w.get("ok"):
        sys.exit(f"run failed: {w.get('message') or w}")
    say(f"base run done ({time.time() - t:.0f} s)")
    return deck_id, a["run_id"]


def parameters(con, deck_id: int) -> list[dict]:
    from opmagent.hm import params as pm
    t = pm.targets(con, deck_id)
    have = {f["fault"] for f in t["faults"]}
    ps = [{"name": f"kx_{g}", "kind": "permx_mult", "target": {"layers": lay}, "low": 0.3, "high": 3.0}
          for g, lay in GROUPS.items()]
    ps += [{"name": f"tf_{f}", "kind": "multflt", "target": {"fault": f}, "low": 0.001, "high": 1.0, "scale": "log"}
           for f in FAULTS if f in have]
    return ps


def _same_setup(con, ps):
    """The NORNE_HM setup of an earlier run of this script, if it has these parameters (its cases are reused)."""
    from opmagent.hm import jobs
    try:
        h = jobs.get_hm(con, "NORNE_HM")
    except Exception:  # noqa: BLE001
        return None
    have = [(p["name"], p["low"], p["high"]) for p in h["parameters"]]
    return h if have == [(p["name"], p["low"], p["high"]) for p in ps] and h["objective"]["sigma"] == \
        {**h["objective"]["sigma"], **OBJECTIVE["sigma"]} else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--norne", type=Path, default=NORNE)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--skip-sensitivity", action="store_true")
    a = ap.parse_args(argv)
    if not a.norne.exists():
        sys.exit(f"{a.norne} not found (tools/fetch_open_data.sh)")
    cfg = workspace(a.out, a.parallel)
    deck_id, run_id = base_deck_and_run(cfg, a.norne)
    from opmagent.hm import jobs, params as pm, service
    con = db.connect(cfg.database)
    out = {"deck_id": deck_id, "base_run_id": run_id, "objective": OBJECTIVE}

    inv = service.observation_inventory(con, run_id=run_id)
    out["observations"] = {"source": inv["source"], "period": inv["period"], "wells": len(inv["wells"]),
                           "points": inv["points"],
                           "controls": sorted({c for w in inv["wells"] for c in w["controls"]})}
    say(f"observations: {inv['source']}, {inv['period'][0]} to {inv['period'][1]}, {len(inv['wells'])} wells, "
        f"controls {', '.join(out['observations']['controls'])}")
    t = pm.targets(con, deck_id)
    spec = pm.deck_models(con, deck_id)["deck"]["spec_json"]
    out["targets"] = {"grid": t["grid"], "faults": len(t["faults"]), "zones": len(t["zones"]),
                      "satnums": len(t["satnums"]), "corey_satnums": sum(not s["tabulated"] for s in t["satnums"]),
                      "aquifers": len(t["aquifers"]),
                      "rewritten_by_the_deck": {kw: pm.rewritten(spec, kw)[:2] for kw in ("PERMX", "PERMZ", "PORO",
                                                                                           "MULTFLT")}}
    base = service.public(service.run_misfit(con, run_id, objective=OBJECTIVE))
    out["base"] = {"misfit": base["misfit"], "points": base["points"], "findings": base["findings"][:12],
                   "worst_terms": [{k: t[k] for k in ("well", "quantity", "n", "misfit", "cum_error_pct") if k in t}
                                   for t in base["terms"][:10]]}
    say(f"base misfit {base['misfit']:.2f} over {base['points']} points")
    for f in base["findings"][:6]:
        say(f"  {f}")
    rep = service.write_report(cfg, con, run_id, objective=OBJECTIVE)
    out["base_report"] = rep["report"]
    say(f"base report: {rep['report']}")

    if not a.skip_sensitivity:
        ps = parameters(con, deck_id)
        h = _same_setup(con, ps)
        if h is None:
            s = jobs.setup(con, "NORNE_HM", deck_id, ps, source="deck_history", objective=OBJECTIVE)
            h = jobs.get_hm(con, s["hm_id"])
        say(jobs.read_back(h))
        t0 = time.time()
        busy = con.execute("SELECT id FROM hm_jobs WHERE hm_id = ? AND status IN ('queued','running')",
                           (h["id"],)).fetchone()
        if busy:      # a run of this script that was stopped: finish that job (its cases are kept)
            con.execute("UPDATE hm_jobs SET status = 'stopped' WHERE id = ?", (busy["id"],))
        job = jobs.create_job(con, h, "sensitivity")
        j = jobs.Runner(cfg, job, log=lambda m: say("  " + m)).run()
        say(jobs.spoken_job(j))
        out["sensitivity"] = {"job": job, "wall_s": round(time.time() - t0), "progress": j["progress"],
                              "result": j["result"], "parameters": ps}
        if j.get("result"):
            for d in j["result"]["tornado"]:
                lo = "failed" if d["misfit_low"] is None else f"{d['misfit_low']:.2f}"
                hi = "failed" if d["misfit_high"] is None else f"{d['misfit_high']:.2f}"
                say(f"  {d['rank']:>2}. {d['name']:<10} low {lo:>8}  high {hi:>8}  "
                    f"best reduction {d['best_reduction']:.2f} ({d['better_end'] or '-'})")
        mr = jobs.write_match_report(cfg, con, h["id"])
        out["match_report"] = mr["report"]
        say(f"match report: {mr['report']}")
    (a.out / "hm_norne.json").write_text(json.dumps(out, indent=1, default=str))
    say(f"numbers: {a.out / 'hm_norne.json'}")


if __name__ == "__main__":
    main()
