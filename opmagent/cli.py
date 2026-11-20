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
        from .core.errors import ToolError
        try:
            q = rv.get_qc_report(kind=args.kind, version_id=args.version_id)
        except ToolError as e:
            raise SystemExit(f"{e}. Run `opmagent review list` to see what is waiting.")
        if q["status"] != "qc_ready":   # decided already (or still a draft): nothing to confirm
            raise SystemExit(f"{args.kind} {q['model_name']} v{q['version']} (id {args.version_id}) is already "
                             f"{q['status']}; nothing to do. Run `opmagent review list` to see what is waiting.")
        targets = [(args.kind, {"version_id": args.version_id})]
    for kind, m in targets:
        q = rv.get_qc_report(kind=kind, version_id=m["version_id"])
        print(f"{args.rcmd.replace('-hello', '')}: {kind} {q['model_name']} v{q['version']} "
              f"(QC report {q['qc_ready_at']}, {(q['qc_report'] or {}).get('path')})")
    import sys
    if not sys.stdin.isatty():
        raise SystemExit("Approvals need an interactive terminal (a person typing). Agents can't approve.")
    word = "reject" if args.rcmd == "reject" else "approve"
    if input(f"Type '{word}' to confirm: ").strip().lower() != word:
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


def cmd_import(args):
    import json
    from .core.errors import ToolError
    from .deck.import_pipeline import import_to_drafts, verify
    cfg = cfgmod.load(args.config)
    try:
        r = import_to_drafts(cfg, args.input, args.name) if args.icmd == "deck" else verify(cfg, args.name)
    except ToolError as e:
        r = e.to_dict()
    print(r.get("spoken_summary", ""))
    print(json.dumps({k: v for k, v in r.items() if k not in ("deck",)}, indent=2, default=str)[:6000])
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_well_build(args):
    import json
    from .core.errors import ToolError
    from .well.pipeline import build_well_model
    cfg = cfgmod.load(args.config)
    files = {"wells": args.wells, "surveys": args.surveys, "perforations": args.perforations, "history": args.history,
             "controls": args.controls, "groups": args.groups, "history_format": args.history_format}
    try:
        r = build_well_model(cfg, {k: v for k, v in files.items() if v}, args.name, start_date=args.start_date,
                             geo_version_id=args.geo_version, unit_system=args.unit_system, description=args.description,
                             allow_gaps=args.allow_gaps)
    except (ToolError, ValueError) as e:
        r = e.to_dict() if isinstance(e, ToolError) else {"ok": False, "code": "INVALID_INPUT", "message": str(e)}
    print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_geo_build(args):
    import json
    from .core.errors import ToolError
    from .geo.pipeline import build_geo_model
    cfg = cfgmod.load(args.config)
    spec = json.loads(open(args.spec).read())
    try:
        r = build_geo_model(cfg, tops=args.tops, logs=args.logs or [], spec=spec, name=args.name, surveys=args.surveys,
                            description=args.description)
    except ToolError as e:
        r = e.to_dict()
    print(json.dumps({k: v for k, v in r.items()}, indent=2, default=str)[:6000])
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_setup(args):
    from pathlib import Path
    from .core import db as dbmod
    from .setup import write_workspace
    repo_skills = Path(__file__).resolve().parent.parent / "skills"
    r = write_workspace(Path(args.workspace), repo_skills=repo_skills, config=args.config, force=args.force)
    cfg = cfgmod.load(r["config"])
    if not cfg.database.exists():
        dbmod.init_db(cfg.database)
    for sub_ in ("inbox", "models", "decks", "qc", "debug", "runs", "imports"):
        (cfg.file_store / sub_).mkdir(parents=True, exist_ok=True)
    print(f"Workspace ready: {r['workspace']}")
    print(f"  config      {r['config']}")
    print(f"  database    {cfg.database}")
    print(f"  inbox       {cfg.inbox}")
    print(f"  MCP servers {', '.join(r['servers'])}  (.mcp.json)")
    print("  agents      .claude/agents/  skills .claude/skills/  rules CLAUDE.md")
    print(f"\nReviewer (YOUR client only): add the server in {r['reviewer']}")
    print("  Claude desktop: merge it into claude_desktop_config.json; Claude Code: `claude mcp add-json opm-reviewer '<json>'`")
    print("  in a session you use yourself, never in the agents workspace.")
    print(f"\nNext: cd {r['workspace']} && opmagent --config {r['config']} doctor && opmagent --config {r['config']} hello")


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="path to opmagent.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the SQLite database and file store").set_defaults(fn=cmd_init_db)
    sub.add_parser("doctor", help="check prerequisites").set_defaults(fn=cmd_doctor)
    x = sub.add_parser("setup", help="create a local agents workspace for Claude Code (MCP servers, agents, skills)")
    x.add_argument("workspace", nargs="?", default="~/opm-agents")
    x.add_argument("--force", action="store_true", help="overwrite .mcp.json instead of merging")
    x.set_defaults(fn=cmd_setup)
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
        x.set_defaults(fn=cmd_review)
    x = rv.add_parser("approve-hello", help="approve the three hello demo models")
    x.set_defaults(fn=cmd_review, comment=None)
    dk = sub.add_parser("deck", help="deck builder").add_subparsers(dest="dcmd", required=True)
    x = dk.add_parser("build", help="build a deck from a JSON spec")
    x.add_argument("--spec", required=True)
    x.set_defaults(fn=cmd_deck_build)
    x = sub.add_parser("run", help="run a validated deck")
    x.add_argument("--deck", required=True)
    x.set_defaults(fn=cmd_run)
    wl = sub.add_parser("well", help="well modeler").add_subparsers(dest="wcmd", required=True)
    x = wl.add_parser("build", help="build a well model from CSV/Excel inputs and submit it for review")
    for a_ in ("wells", "surveys", "perforations", "history", "controls", "groups"):
        x.add_argument(f"--{a_}")
    x.add_argument("--history-format", dest="history_format", choices=["standard", "volve"], default="standard")
    x.add_argument("--name", required=True)
    x.add_argument("--start-date", dest="start_date")
    x.add_argument("--geo-version", dest="geo_version", type=int)
    x.add_argument("--unit-system", dest="unit_system", choices=["METRIC", "FIELD"], default="METRIC")
    x.add_argument("--description")
    x.add_argument("--allow-gaps", action="store_true")
    x.set_defaults(fn=cmd_well_build)
    gm = sub.add_parser("geo", help="geo modeler").add_subparsers(dest="gcmd", required=True)
    x = gm.add_parser("build", help="build a corner-point geo model from tops, logs and a spec")
    x.add_argument("--tops", required=True)
    x.add_argument("--logs", nargs="*", help="LAS or CSV log files")
    x.add_argument("--surveys", help="deviation surveys (to convert LAS MD to TVD)")
    x.add_argument("--spec", required=True, help="geo spec JSON (surfaces, layers, areal, contacts, petro, scal)")
    x.add_argument("--name", required=True)
    x.add_argument("--description")
    x.set_defaults(fn=cmd_geo_build)
    im = sub.add_parser("import", help="deck importer (reverse deck)").add_subparsers(dest="icmd", required=True)
    x = im.add_parser("deck", help="import a deck into fluid/geo/well drafts for review")
    x.add_argument("--input", required=True)
    x.add_argument("--name", required=True)
    x.set_defaults(fn=cmd_import)
    x = im.add_parser("verify", help="after approval: rebuild, run both, compare")
    x.add_argument("--name", required=True)
    x.set_defaults(fn=cmd_import)
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
