"""opmagent command line: setup helpers (servers are started by the MCP client)."""
from __future__ import annotations

import argparse
import shutil
import subprocess

from . import __version__
from .core import config as cfgmod
from .core import db


def cmd_init_db(args):
    cfg = cfgmod.load(args.config)
    db.init_db(cfg.database)
    for sub in ("inbox", "models", "decks", "qc", "debug"):
        (cfg.file_store / sub).mkdir(parents=True, exist_ok=True)
    print(f"database created: {cfg.database}\nfile store: {cfg.file_store}")


def cmd_doctor(args):
    """Check the prerequisites the servers need."""
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"[ok]   {name}: {detail}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name}: {e}")

    cfg = None
    try:
        cfg = cfgmod.load(args.config)
        print(f"[ok]   config: database={cfg.database}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] config: {e}")
    check("java", lambda: next(line for line in subprocess.run(["java", "-version"], capture_output=True, text=True, check=True).stderr.splitlines() if "version" in line))
    check("neqsim", lambda: __import__("neqsim").__name__ and "importable")
    flow = cfg.flow_bin if cfg else "flow"
    check("opm parser (opm.io)", lambda: __import__("opm.io") and "importable")

    def simulator():
        if shutil.which(flow):
            return f"flow binary: {shutil.which(flow)}"
        __import__("opm.simulators")
        return "opm.simulators Python bindings (flow binary not on PATH; optional)"
    check("OPM simulator", simulator)
    if cfg:
        check("database", lambda: "exists" if cfg.database.exists() else (_ for _ in ()).throw(FileNotFoundError("run: opmagent init-db")))
    raise SystemExit(0 if ok else 1)


def cmd_fluid_template(args):
    from .fluid.excel import write_template
    print(f"template written: {write_template(args.out, example=args.example)}")


def cmd_fluid_build(args):
    import json

    from .fluid.excel import read_lab_report
    from .fluid.pipeline import build_fluid_model
    from .core.errors import ToolError
    from .servers.data_server import DataService
    from .servers.pvt_server import PVTService

    cfg = cfgmod.load(args.config)
    fi, gaps = read_lab_report(args.input)
    if args.check:
        print(json.dumps({"gaps": gaps, "fluid_input": fi}, indent=2, default=str))
        return
    opts = {"eos": args.eos, "volume_shift": args.volume_shift, "pseudo_components": args.pseudo_components}
    try:
        r = build_fluid_model(fi, args.name, data=DataService("fluid", cfg), pvt=PVTService(cfg), options=opts,
                              origin=args.origin, user_description=args.description, gaps=gaps,
                              allow_default_stages=args.allow_default_stages,
                              accept_tolerance_miss=args.accept_tolerance_miss)
    except ToolError as e:
        r = e.to_dict()
    print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r.get("ok") else 2)


def _reviewer(cfg):
    from .servers.data_server import DataService
    return DataService("reviewer", cfg)


def cmd_review(args):
    """Human review from the terminal (uses the reviewer profile)."""
    import json
    cfg = cfgmod.load(args.config)
    rv = _reviewer(cfg)
    if args.rcmd == "list":
        print(rv.list_pending_reviews()["spoken_summary"])
        for p in rv.list_pending_reviews()["pending"]:
            q = rv.get_qc_report(kind=p["kind"], version_id=p["version_id"])
            print(f"  {p['kind']:5} id={p['version_id']:<4} {p['model_name']} v{p['version']}  report: {(q['qc_report'] or {}).get('path')}")
        return
    targets = []
    if args.rcmd == "approve-hello":
        from .hello.run import latest_demo
        targets = [(k, m) for k, m in latest_demo(rv.con).items() if m and m["status"] == "qc_ready"]
        if not targets:
            print("No hello models are waiting for review.")
            return
    else:
        targets = [(args.kind, {"version_id": args.version_id})]
    for kind, m in targets:
        q = rv.get_qc_report(kind=kind, version_id=m["version_id"])
        print(f"{args.rcmd.replace('-hello', '')}: {kind} {q['model_name']} v{q['version']} "
              f"(QC report {q['qc_ready_at']}, {(q['qc_report'] or {}).get('path')})")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return
    for kind, m in targets:
        q = rv.get_qc_report(kind=kind, version_id=m["version_id"])
        rid = (q["qc_report"] or {}).get("id")
        if args.rcmd == "reject":
            r = rv.reject(kind=kind, version_id=m["version_id"], qc_report_file_id=rid, comment=args.comment)
        else:
            r = rv.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid,
                           comment="hello demo" if args.rcmd == "approve-hello" else args.comment)
        print(r["spoken_summary"])


def cmd_deck_build(args):
    import json
    from .core.errors import ToolError
    from .deck.build import build_deck
    cfg = cfgmod.load(args.config)
    spec = json.loads(open(args.spec).read())
    try:
        r = build_deck(spec, cfg)
    except ToolError as e:
        r = e.to_dict()
    print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_run(args):
    import json
    from .sim.runner import run_deck, summary_values
    cfg = cfgmod.load(args.config)
    rv = _reviewer(cfg)
    d = next((d for d in reversed(rv.list_decks()["decks"]) if d["name"] == args.deck), None)
    if not d:
        raise SystemExit(f"no deck named {args.deck}")
    deck = rv.get_deck(deck_id=d["id"])["deck"]
    if deck["status"] != "validated":
        raise SystemExit(f"deck {args.deck} is {deck['status']}, not validated")
    path = next(f["path"] for f in deck["files"] if f["role"] == "DATA")
    r = run_deck(path, cfg.file_store / "runs" / f"{args.deck}_deck{d['id']}", cfg.flow_bin)
    if r["ok"]:
        r["summary"] = {k: v["last"] for k, v in summary_values(r["smspec"], ["FOPR", "FPR", "FWCT", "FOPT"]).items()
                        if isinstance(v, dict)}
    print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r["ok"] else 2)


def cmd_hello(args):
    import json
    from .hello.run import clean, hello
    cfg = cfgmod.load(args.config)
    r = clean(cfg) if args.clean else hello(cfg)
    print(r["spoken_summary"])
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r.get("ok") else 2)


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="path to opmagent.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the SQLite database and file store").set_defaults(fn=cmd_init_db)
    sub.add_parser("doctor", help="check prerequisites").set_defaults(fn=cmd_doctor)
    fl = sub.add_parser("fluid", help="fluid modeler").add_subparsers(dest="fcmd", required=True)
    t = fl.add_parser("template", help="write the Excel lab report template")
    t.add_argument("out")
    t.add_argument("--example", action="store_true", help="fill in the built-in demo oil")
    t.set_defaults(fn=cmd_fluid_template)
    b = fl.add_parser("build", help="build a fluid model from an Excel lab report and submit it for review")
    b.add_argument("--input", required=True)
    b.add_argument("--name", help="model name, e.g. FieldA_Oil")
    b.add_argument("--description")
    b.add_argument("--eos", choices=["PR", "PR78", "SRK"])
    b.add_argument("--volume-shift", dest="volume_shift", action=argparse.BooleanOptionalAction, default=None)
    b.add_argument("--pseudo-components", dest="pseudo_components", type=int)
    b.add_argument("--origin", choices=["built", "demo"], default="built")
    b.add_argument("--allow-default-stages", action="store_true")
    b.add_argument("--accept-tolerance-miss", action="store_true")
    b.add_argument("--check", action="store_true", help="only read the workbook and list gaps")
    b.set_defaults(fn=cmd_fluid_build)
    rv = sub.add_parser("review", help="human review (approve / reject)").add_subparsers(dest="rcmd", required=True)
    rv.add_parser("list", help="what is waiting for review").set_defaults(fn=cmd_review)
    for name in ("approve", "reject"):
        x = rv.add_parser(name)
        x.add_argument("kind", choices=["fluid", "geo", "well"])
        x.add_argument("version_id", type=int)
        x.add_argument("--comment", required=name == "reject")
        x.add_argument("--yes", action="store_true")
        x.set_defaults(fn=cmd_review)
    x = rv.add_parser("approve-hello", help="approve the three hello demo models")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(fn=cmd_review, comment=None)
    dk = sub.add_parser("deck", help="deck builder").add_subparsers(dest="dcmd", required=True)
    x = dk.add_parser("build", help="build a deck from a JSON spec")
    x.add_argument("--spec", required=True)
    x.set_defaults(fn=cmd_deck_build)
    x = sub.add_parser("run", help="run a validated deck")
    x.add_argument("--deck", required=True)
    x.set_defaults(fn=cmd_run)
    x = sub.add_parser("hello", help="end-to-end demo: models, approval, deck, run")
    x.add_argument("--clean", action="store_true")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_hello)
    args = p.parse_args(argv)
    if getattr(args, "fcmd", None) == "build" and not args.check and not args.name:
        p.error("--name is required")
    args.fn(args)


if __name__ == "__main__":
    main()
