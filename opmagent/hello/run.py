"""`opmagent hello`: end-to-end smoke test with demo data.

Idempotent stages, each run continues where the last one stopped:
  1. build HELLO_FLUID / HELLO_GEO / HELLO_RES / HELLO_WELLS (demo) and submit them for review
  2. wait for a human: `opmagent review approve-hello`
  3. build the HELLO deck from the approved demo models, parse it with OPM
  4. run it and report field oil rate and pressure
"""
from __future__ import annotations

from ..core.models import list_models
from ..deck.build import build_deck
from ..servers.data_server import DataService
from ..sim.runner import run_deck, summary_values
from . import models as hm

KINDS = (("fluid", hm.FLUID), ("geo", hm.GEO), ("res", hm.RES), ("well", hm.WELLS))


def latest_demo(con) -> dict:
    out = {}
    for kind, name in KINDS:
        rows = [m for m in list_models(con, kind) if m["model_name"] == name and m["origin"] == "demo"]
        out[kind] = max(rows, key=lambda m: m["version"]) if rows else None
    return out


def clean(cfg) -> dict:
    """Delete demo versions still in draft or under review. Approved demo versions stay (never deleted)."""
    con = DataService("reviewer", cfg).con
    removed = []
    for kind, name in sorted(KINDS, key=lambda k: ("well", "res", "fluid", "geo").index(k[0])):  # well, res reference geo
        vt = f"{kind}_model_versions"
        for m in list_models(con, kind):
            if m["model_name"] == name and m["origin"] == "demo" and m["status"] in ("draft", "qc_ready"):
                if m["status"] == "qc_ready":
                    con.execute(f"UPDATE {vt} SET status='draft' WHERE id=?", (m["version_id"],))
                con.execute(f"DELETE FROM {vt} WHERE id=?", (m["version_id"],))
                removed.append(f"{kind} {name} v{m['version']}")
    return {"ok": True, "removed": removed,
            "spoken_summary": f"Removed {len(removed)} demo draft(s)." if removed else "Nothing to clean."}


def hello(cfg, *, log=print) -> dict:
    stages = []

    def stage(name, ok, detail=""):
        stages.append({"stage": name, "ok": ok, "detail": detail})
        log(f"[{'pass' if ok else 'FAIL' if ok is False else 'wait'}] {name}{': ' + detail if detail else ''}")

    con = DataService("reviewer", cfg).con
    cur = latest_demo(con)
    usable = lambda m: m and m["status"] in ("qc_ready", "approved")  # noqa: E731
    try:
        if not usable(cur["fluid"]):
            r = hm.build_fluid(cfg)
            stage("fluid model (NeqSim)", True, f"{hm.FLUID} v{r['version']}, Psat {r['metrics']['psat_model']:.0f} bar")
        else:
            stage("fluid model (NeqSim)", True, f"{hm.FLUID} v{cur['fluid']['version']} ({cur['fluid']['status']})")
        geo_rebuilt = False
        if not usable(cur["geo"]):
            r = hm.build_geo(cfg)
            geo_rebuilt = True
            stage("geo model", True, f"{hm.GEO} v{r['version']}")
        else:
            stage("geo model", True, f"{hm.GEO} v{cur['geo']['version']} ({cur['geo']['status']})")
        cur = latest_demo(con)
        if geo_rebuilt or not usable(cur["res"]):
            r = hm.build_res(cfg, cur["geo"]["version_id"])
            if not r["ok"]:
                raise RuntimeError(r["spoken_summary"])
            stage("res model", True, f"{hm.RES} v{r['version']}")
        else:
            stage("res model", True, f"{hm.RES} v{cur['res']['version']} ({cur['res']['status']})")
        if geo_rebuilt or not usable(cur["well"]):
            r = hm.build_wells(cfg, cur["geo"]["version_id"])
            stage("well model", True, f"{hm.WELLS} v{r['version']}")
        else:
            stage("well model", True, f"{hm.WELLS} v{cur['well']['version']} ({cur['well']['status']})")
    except Exception as e:  # noqa: BLE001
        stage("models", False, str(e))
        return {"ok": False, "stages": stages, "spoken_summary": f"Hello failed while building models: {e}"}

    cur = latest_demo(con)
    waiting = [f"{k} {m['model_name']} v{m['version']}" for k, m in cur.items() if m["status"] != "approved"]
    if waiting:
        stage("your approval", None, "waiting for " + ", ".join(waiting))
        return {"ok": True, "stage": "waiting_for_approval", "stages": stages,
                "spoken_summary": "The four hello models are waiting for your review. Say approve hello, "
                                  "or run: opmagent review approve-hello. Then run hello again."}
    stage("your approval", True, "all four demo models approved")

    try:
        d = build_deck({"name": "HELLO", "requirement": "hello demo: 5-year waterflood prediction",
                        "fluid": cur["fluid"]["version_id"], "geo": cur["geo"]["version_id"],
                        "res": cur["res"]["version_id"], "well": cur["well"]["version_id"], "years": 5, "report": "monthly"}, cfg, origin="demo")
    except Exception as e:  # noqa: BLE001
        stage("deck build", False, str(e))
        return {"ok": False, "stages": stages, "spoken_summary": f"Hello failed building the deck: {e}"}
    stage("deck build + OPM parse", d["ok"], d["path"] if d["ok"] else d["validation_log"][-300:])
    if not d["ok"]:
        return {"ok": False, "stages": stages, "deck": d, "spoken_summary": d["spoken_summary"]}

    r = run_deck(d["path"], cfg.file_store / "runs" / f"HELLO_deck{d['deck_id']}", cfg.flow_bin)
    if not r["ok"]:
        stage("OPM Flow run", False, r.get("message") or r["log_tail"][-300:])
        return {"ok": False, "stages": stages, "deck": d, "run": r,
                "spoken_summary": "The deck is valid but the run failed; see the run log."}
    v = summary_values(r["smspec"], ["FOPR", "FPR", "FWCT", "FOPT"])
    stage("OPM Flow run", True, f"{r['engine']}, {r['seconds']} s; after 5 years FOPR {v['FOPR']['last']:.0f} sm3/d, "
                                f"FPR {v['FPR']['last']:.0f} bar, water cut {v['FWCT']['last']*100:.0f}%")
    return {"ok": True, "stage": "done", "stages": stages, "deck": d, "run": r,
            "results": {k: v[k]["last"] for k in ("FOPR", "FPR", "FWCT", "FOPT")},
            "spoken_summary": f"Hello passed. The demo deck ran for 5 years: oil rate {v['FOPR']['last']:.0f} cubic metres "
                              f"a day at the end, pressure {v['FPR']['last']:.0f} bar."}
