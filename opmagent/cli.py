"""opmagent command line: setup helpers (servers are started by the MCP client)."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from . import __version__
from .core import config as cfgmod
from .core import db

MODEL_KINDS = ("fluid", "geo", "res", "well")   # res: the dynamic model (SCAL, rock, contacts, regions) on a geo grid


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
        cp = cfgmod.find_path(args.config)
        import os
        why = ("--config" if args.config else "OPMAGENT_CONFIG" if os.environ.get("OPMAGENT_CONFIG") else
               "the default workspace, ~/.config/opmagent/opmagent.toml" if cp and cfgmod.USER_CONFIG.is_file()
               and cp == cfgmod.USER_CONFIG.resolve() else "this folder")
        print(f"[ok]   config: database={cfg.database}  ({cp}, from {why})")
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

    def resinsight():
        from .viewer import find_resinsight
        exe = find_resinsight(cfg.resinsight_bin if cfg else None)
        if exe:
            return exe
        raise FileNotFoundError("not found (optional, to view runs): install it in /opt/ResInsight, or set "
                                "[viewer] resinsight_bin")
    try:
        print(f"[ok]   ResInsight: {resinsight()}")
    except FileNotFoundError as e:
        print(f"[warn] ResInsight: {e}")
    if cfg:
        check("database", lambda: "exists" if cfg.database.exists() else (_ for _ in ()).throw(FileNotFoundError("run: opmagent init-db")))

    # the agents: the workspace is --workspace, or the folder of the config when setup made it (it has .mcp.json)
    from .doctor import agents_report, format_report
    cp = cfgmod.find_path(args.config)
    ws = Path(args.workspace).expanduser() if args.workspace else (cp.parent if cp else None)
    if ws is not None and (ws / ".mcp.json").is_file():
        repo_skills = Path(__file__).resolve().parent.parent / "skills"
        r = agents_report(ws, cfg, repo_skills, deep=args.deep)
        if args.json:
            import json
            print(json.dumps(r, indent=2, default=str))
        else:
            print(format_report(r, verbose=args.verbose))
        ok = ok and r["status"] != "fail"
    elif args.workspace:
        ok = False
        print(f"[FAIL] agents: {ws} has no .mcp.json (not a workspace made by opmagent setup)")
    else:
        print("[info] agents: no workspace found next to the config (run in the workspace, or pass --workspace)")
    raise SystemExit(0 if ok else 1)


def cmd_res_template(args):
    from .res.scal_excel import write_template
    print(f"SCAL template written: {write_template(args.out, example=args.example)}  (put it in the study's inbox, "
          "then opmagent data load)")


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


def cmd_model_notebook(args):
    """QC notebooks (fluid or geo): re-run the standard QC, start or run an investigation (same rules as the
    MCP tools)."""
    import json
    import subprocess

    from .servers.data_server import DataService, wrap

    cfg = cfgmod.load(args.config)
    kind = getattr(args, "nb_kind", "fluid")
    svc = DataService(kind, cfg)
    cmd = {"fluid": "fcmd", "geo": "gcmd", "res": "rscmd", "well": "wcmd"}[kind]
    cmd = getattr(args, cmd)
    if cmd == "qc":
        r = wrap(svc, "run_qc_notebook")(version_id=args.version_id)
    elif cmd == "investigate":
        r = wrap(svc, "create_investigation_notebook")(version_id=args.version_id, purpose=args.purpose)
        if r.get("ok") and args.open:
            try:
                subprocess.Popen([cfg.vscode_bin, r["path"]])
            except OSError as e:
                r["open_error"] = str(e)
    else:
        r = wrap(svc, "run_investigation_notebook")(path=args.path)
    print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r.get("ok") else 2)


def _reviewer(cfg):
    from .servers.data_server import DataService
    return DataService("reviewer", cfg)


def _hm_review(rv, args):
    """The engineer's decision on a match proposal: show, accept (requests to the modelers) or reject."""
    from .core.errors import ToolError
    from .hm import proposals
    try:
        if args.action == "show":
            p = proposals.show(rv.con, args.proposal_id)
            print(f"Proposal {p['id']} for history match {p['hm']['name']} (revision {p['hm']['revision']}, deck "
                  f"{p['hm']['deck_id']}{', study ' + p['hm']['study'] if p['hm']['study'] else ''}): {p['status']}")
            print(f"  misfit {p['misfit']:.4g}" + (f" (base {p['base_misfit']:.4g})" if p["base_misfit"] is not None
                                                  else "") + f", case {p['case_no']}")
            for owner, lines in ((p["requests"] or {}).get("changes") or {}).items():
                for line in lines:
                    print(f"  {owner}: {line}")
            f = p["flags"] or {}
            if f.get("at_bound"):
                print(f"  at a bound of its range: {', '.join(f['at_bound'])}")
            if f.get("wells_worse"):
                print(f"  wells worse than the base case: {', '.join(f['wells_worse'])}")
            if p.get("report_path"):
                print(f"  report: {p['report_path']}")
            if p["status"] == "proposed":
                print(f"  decide: opmagent review hm accept {p['id']}   or   opmagent review hm reject {p['id']} "
                      "--comment '...'")
            return
        r = proposals.decide(rv.con, args.proposal_id, args.action, comment=args.comment)
    except ToolError as e:
        raise SystemExit(f"{e.code}: {e.message}") from None
    if args.action == "accept":
        print(f"Proposal {args.proposal_id} accepted.")
        for q in r["raised"]:
            print(f"  request {q['request_id']} to {q['to']}: {q['message']}")
        if not r["raised"]:
            print("  No study: give these changes to the modelers yourself:")
            for owner, lines in ((r["requests"] or {}).get("changes") or {}).items():
                print(f"    {owner}: {'; '.join(lines)}")
    else:
        print(f"Proposal {args.proposal_id} rejected; the comment goes back to the history matcher.")


def cmd_review(args):
    """Human review from the terminal (uses the reviewer profile)."""
    import json
    cfg = cfgmod.load(args.config)
    rv = _reviewer(cfg)
    if args.rcmd == "comment":
        from .core.errors import ToolError
        try:
            r = rv.add_review_comment(kind=args.kind, version_id=args.version_id, comment=args.text, item=args.item,
                                      severity=args.severity)
        except ToolError as e:
            raise SystemExit(str(e))
        print(r["spoken_summary"])
        return
    if args.rcmd == "feedback":
        items = rv.get_review_feedback(kind=args.kind, model_name=args.model, status=None if args.all else "open")
        print(items["spoken_summary"] if not items["feedback"] else f"{len(items['feedback'])} comment(s):")
        for f in items["feedback"]:
            ans = (f"  -> {f['status']}" + (f" in version id {f['addressed_in_version_id']}"
                                            if f["addressed_in_version_id"] else "") + f": {f['response']}"
                   if f["status"] != "open" else "")
            print(f"  #{f['id']:<4} {f['model_kind']:5} {f['model_name']} v{f['version']} ({f['severity']}"
                  f"{', ' + f['item'] if f['item'] else ''}) {f['comment']}{ans}"[:300])
        return
    if args.rcmd == "list":
        from .deck.init_review import pending as init_pending
        for p in init_pending(rv.con):
            print(f"  deck  id={p['id']:<4} {p['name']}: initial state waiting (opmagent review accept-init {p['id']})")
        print(rv.list_pending_reviews()["spoken_summary"])
        for p in rv.list_pending_reviews()["pending"]:
            q = rv.get_qc_report(kind=p["kind"], version_id=p["version_id"])
            print(f"  {p['kind']:5} id={p['version_id']:<4} {p['model_name']} v{p['version']}  report: {(q['qc_report'] or {}).get('path')}")
        return
    if args.rcmd in ("accept-init", "reject-init"):
        return _init_review(cfg, rv, args)
    if args.rcmd == "hm":
        return _hm_review(rv, args)
    targets = []
    if args.rcmd == "approve-hello":
        from .hello.run import latest_demo
        targets = [(k, m) for k, m in latest_demo(rv.con).items() if m and m["status"] == "qc_ready"]
        if not targets:
            from .deck.init_review import pending
            hello_deck = next((p for p in pending(rv.con) if p["name"] == "HELLO"), None)
            if hello_deck:   # the models are approved; what waits now is the hello deck's initial state
                args.deck_id, args.comment = hello_deck["id"], "hello demo"
                return _init_review(cfg, rv, args, accept=True)
            print("No hello models or hello initial state are waiting for review.")
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


def _init_review(cfg, rv, args, accept: bool | None = None) -> None:
    """Accept or reject a deck's initial state, after showing it; a person typing at a terminal."""
    import sys
    from .core.errors import ToolError
    from .deck import init_review as ir
    accept = (args.rcmd == "accept-init") if accept is None else accept
    try:
        view, _ = ir._view(cfg, rv.con, args.deck_id)
    except ToolError as e:
        raise SystemExit(str(e))
    if ir.state(rv.con, args.deck_id) != "waiting":
        r = ir.latest(rv.con, args.deck_id)
        raise SystemExit(f"deck {args.deck_id}'s initial state was already {r['decision']} by {r['reviewer']}; "
                         "nothing to do.")
    print(f"Initial state of deck {args.deck_id} ({view.get('deck')}):")
    for line in ir.summary_lines(view):
        print(f"  {line}")
    print(f"  look at it in 3D: opmagent results open --deck {args.deck_id} --init")
    if not sys.stdin.isatty():
        raise SystemExit("Accepting an initial state needs an interactive terminal (a person typing). "
                         "Agents can't accept it.")
    word = "accept" if accept else "reject"
    if input(f"Type '{word}' to confirm: ").strip().lower() != word:
        print("Cancelled.")
        return
    try:
        r = (ir.accept(cfg, rv.con, args.deck_id, rv._reviewer(), args.comment) if accept else
             ir.reject(cfg, rv.con, args.deck_id, rv._reviewer(), args.comment, args.to))
    except ToolError as e:
        raise SystemExit(str(e))
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


def cmd_deck_init(args):
    """The initialisation view of a deck: initial state and fluids in place at time zero (OPM Flow, no wells)."""
    import json
    from .core.errors import ToolError
    from .deck.init_view import build_init_view
    from .viewer import open_in_resinsight
    cfg = cfgmod.load(args.config)
    con = db.connect(cfg.database)
    try:
        r = build_init_view(cfg, con, args.deck_id)
    except ToolError as e:
        r = e.to_dict()
    if r.get("ok") and args.open:
        o = open_in_resinsight(cfg, case=r["egrid"])
        r["spoken_summary"] += " " + o["spoken_summary"]
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(r.get("spoken_summary") or r.get("message"))
        for c in r.get("checks", []):
            print(f"  [{c['status']}] {c['check']}: {c['detail']}")
        for reg, v in sorted((r.get("regions") or {}).items(), key=lambda kv: int(kv[0])):
            print(f"  region {reg}: " + ", ".join(f"{k} {x:,.6g}" for k, x in v.items()))
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_deck_compare(args):
    """Are two decks the same simulation deck? Grid, tables, cell arrays and schedule, without running either."""
    import json
    from .deck.deck_compare import compare_decks
    r = compare_decks(args.original, args.generated)
    print(r["spoken_summary"])
    for s_ in r["sections"]:
        print(f"  {s_['section']:<9} {'same' if s_['passed'] else 'DIFFERENT'}")
        for d in s_["differences"][:10]:
            print(f"      {d}"[:220])
        if s_.get("output_only_differences"):
            print(f"      output-only keywords differ (not compared): {', '.join(s_['output_only_differences'])}")
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    raise SystemExit(0 if r["passed"] else 2)


def _progress_printer(label: str = ""):
    """on_progress callback for runner.run_deck: one refreshing line on a terminal, a line per report step otherwise.
    Writes to stderr, so JSON on stdout stays clean."""
    import sys
    tty = sys.stderr.isatty()
    state = {"step": None}

    def show(p):
        text = f"{label}{p['line']}"
        if tty:
            sys.stderr.write("\r\033[K" + text + ("\n" if p["state"] == "finished" else ""))
        elif p.get("report_step") != state["step"] or p["state"] == "finished" or p.get("stalled"):
            sys.stderr.write(text + "\n")
        sys.stderr.flush()
        state["step"] = p.get("report_step")
    return show


def _flow_process(out_dir: Path) -> tuple[int | None, float]:
    """(pid, seconds running) of a Flow process whose --output-dir is out_dir, from /proc; (None, 0) if none."""
    import os
    want = f"--output-dir={out_dir}"
    for d in Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            if want.encode() in argv or str(out_dir).encode() + b"/" in b" ".join(argv):
                if b"flow" in Path(argv[0].decode()).name.encode():
                    ticks = int((d / "stat").read_text().rsplit(")", 1)[1].split()[19])
                    uptime = float(Path("/proc/uptime").read_text().split()[0])
                    return int(d.name), uptime - ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            continue
    return None, 0.0


def cmd_runs(args):
    """List runs, or follow one live (a run id, a run folder or a run.log; default: the latest run)."""
    import os
    import sys
    import time
    from datetime import datetime
    from .sim.runner import STALL_S, progress
    cfg = cfgmod.load(args.config)
    con = _reviewer(cfg).con
    rows = [dict(r) for r in con.execute("SELECT r.*, d.name AS deck_name FROM runs r JOIN decks d ON d.id=r.deck_id "
                                         "ORDER BY r.id DESC")]
    if args.rcmd == "list":
        if not rows:
            print("No runs yet (runs started by the run agent are listed here).")
        for r in rows:
            print(f"  run {r['id']:<4} {r['deck_name']:<20} {r['status']:<8} started {r['started_at'] or '-'}  "
                  f"finished {r['finished_at'] or '-'}  {r['log_path'] or ''}")
        return
    target, pid, started, finished = args.run, None, None, None
    if target is None or str(target).isdigit():
        r = next((r for r in rows if target is None or r["id"] == int(target)), None)
        if not r:
            raise SystemExit(f"no run {target}" if target else "no runs yet")
        log, pid = Path(r["log_path"] or ""), r["pid"] if r["status"] == "running" else None
        started = datetime.fromisoformat(r["started_at"]) if r["started_at"] else None
        finished = datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None
        print(f"run {r['id']} of deck {r['deck_name']} ({r['status']}), log {log}", file=sys.stderr)
    else:   # a folder or log (e.g. an import verify run): find the Flow process writing there, for elapsed/ETA
        p = Path(target).expanduser().resolve()
        log = p / "run.log" if p.is_dir() else p
        if not log.exists():
            raise SystemExit(f"no run log at {log}")
        pid, age = _flow_process(log.parent)
        if pid:
            started = datetime.fromtimestamp(time.time() - age)
            print(f"Flow pid {pid} is writing {log}", file=sys.stderr)
        elif progress(log)["state"] != "finished":
            print(f"No Flow process is writing {log.parent} and its log has no 'End of simulation': "
                  f"the run was stopped or failed. Last lines:", file=sys.stderr)
            print("\n".join(log.read_text(errors="replace").splitlines()[-15:]), file=sys.stderr)
            raise SystemExit(2)

    def alive():
        if not pid:
            return None   # unknown (a folder given): stop on 'End of simulation'
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    show = _progress_printer()
    try:
        while True:
            pr = progress(log, ((finished or datetime.now()) - started).total_seconds() if started else None)
            show(pr)
            if pr["state"] == "finished":
                break
            if alive() is False:
                print(f"\nFlow (pid {pid}) is no longer running and the log has no 'End of simulation': "
                      f"the run failed or was stopped. Last lines:", file=sys.stderr)
                print("\n".join(log.read_text(errors="replace").splitlines()[-15:]), file=sys.stderr)
                raise SystemExit(2)
            if args.once:
                print(file=sys.stderr)
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped watching (the run itself keeps going)", file=sys.stderr)
    if pr.get("stalled"):
        print(f"no new log output for over {STALL_S} s: check `top` for flow, or the log {log}", file=sys.stderr)


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
    from .core.errors import ToolError
    from .deck.init_review import require
    try:
        require(rv.con, d["id"])
    except ToolError as e:
        raise SystemExit(str(e))
    path = next(f["path"] for f in deck["files"] if f["role"] == "DATA")
    out = cfg.file_store / "runs" / f"{args.deck}_deck{d['id']}"
    import sys
    print(f"running {args.deck} in {out} (log: {out / 'run.log'})", file=sys.stderr)
    r = run_deck(path, out, cfg.flow_bin, on_progress=_progress_printer())
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
        if args.icmd == "deck":
            r = import_to_drafts(cfg, args.input, args.name)
        else:
            r = verify(cfg, args.name, on_progress=lambda label: _progress_printer(f"{label}: "))
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


def cmd_geo_view(args):
    """The static view of a geo version: a static deck made into EGRID + INIT by an OPM Flow dry run."""
    import json
    from .core.errors import ToolError
    from .geo.static_view import build_static_view, latest_res_on
    from .viewer import open_in_resinsight
    cfg = cfgmod.load(args.config)
    con = db.connect(cfg.database)
    res = None if args.no_res else (args.res if args.res is not None else latest_res_on(con, args.version_id))
    try:
        r = build_static_view(cfg, con, args.version_id, res_version_id=res)
    except ToolError as e:
        r = e.to_dict()
    if r.get("ok") and args.open:
        o = open_in_resinsight(cfg, case=r["egrid"])
        r["opened"] = o["ok"]
        r["spoken_summary"] += " " + o["spoken_summary"]
    print(json.dumps(r, indent=2, default=str) if args.json else r.get("spoken_summary") or r.get("message"))
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_res_build(args):
    import json
    from .core.errors import ToolError
    from .res.pipeline import build_res_model
    cfg = cfgmod.load(args.config)
    spec = json.loads(open(args.spec).read())
    if args.scal:
        from .res.scal_excel import read_scal
        types, _unit, gaps = read_scal(args.scal)
        for g in gaps:
            print(f"[{g['severity']}] {g['item']}: {g['message']}")
        if any(g["severity"] == "required" for g in gaps):
            raise SystemExit(f"{args.scal}: fix the required items first")
        spec["scal"] = types
    try:
        r = build_res_model(cfg, spec=spec, name=args.name, geo_version_id=args.geo_version, description=args.description)
    except ToolError as e:
        r = e.to_dict()
    print(json.dumps(r, indent=2, default=str)[:6000])
    raise SystemExit(0 if r.get("ok") else 2)


def cmd_study(args):
    """Studies: init / status / list / events, and build a model or the deck from the study's data."""
    import json
    import getpass
    from .core import db as dbmod, studies
    from .core.errors import ToolError
    cfg = cfgmod.load(args.config)
    con = dbmod.connect(cfg.database)
    try:
        if args.scmd == "init":
            r = studies.init_study(con, cfg.studies_dir, args.name, args.requirement, actor=getpass.getuser(),
                                   years=args.years, end_date=args.end_date, start_date=args.start_date,
                                   report=args.report, unit_system=args.unit_system,
                                   summary=args.summary.split(",") if args.summary else None)
            print(f"Study {args.name} created: {r['study']['folder']}\nPut its raw data in "
                  f"{r['study']['folder']}/inbox; the data manager is next.")
            return
        if args.scmd == "list":
            for s in studies.list_studies(con):
                print(f"  {s['name']:<24} {s['status']:<9} {s['tasks_done']}/{s['tasks']} tasks done  "
                      f"updated {s['updated_at']}")
            return
        if args.scmd == "events":
            for e in studies.events(con, args.name, args.limit):
                print(f"  {e['at']}  {(e['task'] or '-'):<6} {e['event']:<10} {e['actor'] or '':<8} {e['detail'] or ''}"[:200])
            return
        if args.scmd == "status":
            st = studies.status(con, args.name)
            if args.json:
                print(json.dumps(st, indent=2, default=str))
                return
            print(st["spoken_summary"])
            for t in st["tasks"]:
                res = json.dumps(t["result"]) if t["result"] else ""
                print(f"  {t['task']:<6} {studies.AGENT[t['task']]:<15} {t['status']:<11} {res}{('  ' + t['note']) if t['note'] else ''}"[:220])
            for r in st["open_requests"]:
                print(f"  request {r['id']}: {r['from_task']} -> {r['to_task']} ({r['kind']}): {r['message']}")
            return
        if args.scmd == "compare":
            from .data.deck_source import compare_with_source
            r = compare_with_source(cfg, con, args.name, deck_only=args.deck_only)
            print(r["spoken_summary"])
            for s_ in r["deck"]["sections"]:
                print(f"  deck {s_['section']:<9} {'same' if s_['passed'] else 'DIFFERENT'}"
                      + ("" if s_["passed"] else "  " + "; ".join(s_["differences"][:3]))[:200])
            for lv in (r.get("comparison") or {}).get("levels", []):
                print(f"  run  {lv.get('level', '?'):<9} {'same' if lv['passed'] else 'DIFFERENT'}")
            print(f"  report: {r.get('report')}")
            raise SystemExit(0 if r["ok"] else 2)
        if args.scmd == "task":
            res = json.loads(args.result) if args.result else {}
            t = (studies.start_task(con, args.name, args.task, actor=getpass.getuser()) if args.action == "start" else
                 studies.complete_task(con, args.name, args.task, res, actor=getpass.getuser(),
                                       auto_approve=cfg.auto_approve))
            print(json.dumps(t, indent=2, default=str)[:3000])
            return
        if args.scmd == "run":
            r = _study_run(cfg, con, args.name, args.timeout)
            print(r["spoken_summary"])
            raise SystemExit(0 if r["ok"] else 2)
        if args.scmd == "view":
            r = _study_view(con, args.name, args.kinds or list(MODEL_KINDS), args.open, cfg)
            for v in r["views"]:
                print(f"  {v['kind']:<6} {v.get('model', ''):<20} {v.get('status', ''):<10} {v.get('qc_report') or v['note']}")
                if v.get("resinsight"):
                    print(f"         static view (ResInsight): {v['resinsight']}")
            raise SystemExit(0 if r["ok"] else 2)
        if args.complete:
            studies.start_task(con, args.name, args.what, actor=getpass.getuser())
        r = _study_build(cfg, con, args.what, args.name)
        if args.complete and r.get("ok"):
            key = "deck_id" if args.what == "deck" else "version_id"
            from .deck.init_review import state as init_state
            if args.what == "deck" and init_state(con, r["deck_id"]) != "accepted":
                r["task"] = (f"not completed: deck {r['deck_id']}'s initial state waits for you. Look at it "
                             f"(`opmagent results open --deck {r['deck_id']} --init`), accept it "
                             f"(`opmagent review accept-init {r['deck_id']}`), then `opmagent study task {args.name} "
                             f"deck complete --result '{{\"deck_id\": {r['deck_id']}}}'`")
            elif args.what != "deck" and r.get("status") != "approved":
                r["task"] = (f"not completed: version {r['version_id']} is {r.get('status')}; approve it "
                             f"(`opmagent review approve {args.what} {r['version_id']}`), then "
                             f"`opmagent study task {args.name} {args.what} complete --result "
                             f"'{{\"version_id\": {r['version_id']}}}'`")
            else:
                studies.complete_task(con, args.name, args.what, {key: r[key]}, actor=getpass.getuser(),
                                      auto_approve=cfg.auto_approve)
                r["task"] = f"{args.what} task completed"
    except ToolError as e:
        r = e.to_dict()
    print(json.dumps(r, indent=2, default=str)[:8000])
    raise SystemExit(0 if r.get("ok") else 2)


def _study_run(cfg, con, name, timeout_s=3600):
    """The run agent's step from a terminal: start the run task, run the study's deck in Flow (registered in runs),
    store its results and complete the task with the run id."""
    import getpass
    import sys
    from .core import studies
    from .servers.sim_server import SimService
    from .servers.sim_server import wrap as swrap
    st = studies.status(con, name)
    deck_id = ({t["task"]: t["result"] for t in st["tasks"]}.get("deck") or {}).get("deck_id")
    if not deck_id:
        return {"ok": False, "spoken_summary": f"Study {name} has no deck yet: the deck task isn't done."}
    studies.start_task(con, name, "run", actor=getpass.getuser())
    sim = SimService(cfg)
    r = swrap(sim, "run_simulation")(deck_id=int(deck_id), note=f"study {name}")
    if not r.get("ok", True) or "run_id" not in r:
        return {"ok": False, "spoken_summary": r.get("message", "the run didn't start")}
    print(f"run {r['run_id']} started (deck {deck_id}); follow it with `opmagent runs watch {r['run_id']}`",
          file=sys.stderr)
    w = swrap(sim, "wait")(run_id=r["run_id"], timeout_s=int(timeout_s))
    if not w.get("ok"):
        return {"ok": False, "spoken_summary": w.get("spoken_summary") or w.get("message", "the run failed")}
    studies.complete_task(con, name, "run", {"run_id": r["run_id"]}, actor=getpass.getuser())
    return {"ok": True, "run_id": r["run_id"],
            "spoken_summary": f"Run {r['run_id']} finished and stored; the run task is done. "
                              + (w.get("spoken_summary") or "")}


def _study_view(con, name, kinds, open_=False, cfg=None):
    """Each model of the study as its QC report (plots and tables of what the model holds), before any simulation:
    the version the modeler completed its task with, or the study's latest version while it waits for review."""
    import webbrowser
    from .core import models, studies
    st = studies.status(con, name)
    res = {t["task"]: t["result"] for t in st["tasks"]}
    views = []
    for kind in kinds:
        vid = (res.get(kind) or {}).get("version_id")
        if not vid:
            k = models.KINDS[kind]
            r = con.execute(f"SELECT v.id FROM {k['versions']} v JOIN {k['models']} m ON m.id = v.{k['fk']} "
                            f"WHERE m.name = ? ORDER BY v.version DESC LIMIT 1",
                            (st["study"]["spec"]["models"][kind],)).fetchone()
            vid = r["id"] if r else None
        if not vid:
            views.append({"kind": kind, "note": "not built yet"})
            continue
        q = models.get_qc_report(con, kind, int(vid))
        path = (q["qc_report"] or {}).get("path")
        views.append({"kind": kind, "version_id": int(vid), "model": f"{q['model_name']} v{q['version']}",
                      "status": q["status"], "qc_report": path, "note": "" if path else "no QC report"})
        if kind == "geo":
            from .geo.static_view import static_case
            sc = static_case(cfg, con, int(vid)) if cfg is not None else None
            views[-1]["static_view"] = str(sc) if sc else None
            views[-1]["resinsight"] = f"opmagent results open --geo {int(vid)}"
        if open_ and path:
            webbrowser.open(Path(path).as_uri())
    did = (res.get("deck") or {}).get("deck_id")
    if did:
        from .deck.init_view import init_case
        ic = init_case(cfg, con, int(did)) if cfg is not None else None
        views.append({"kind": "deck", "deck_id": int(did), "qc_report": str(ic) if ic else None,
                      "note": "" if ic else "no initialisation view yet: opmagent deck init " + str(did),
                      "resinsight": f"opmagent results open --deck {int(did)} --init", "model": "initialisation view"})
    return {"ok": all(v.get("qc_report") for v in views if v["kind"] != "deck"), "views": views}


def _study_build(cfg, con, what, name):
    """Run a modeler or the deck builder on the study's data in the database, with the study's names."""
    import json
    from .core import studies
    from .core.errors import INVALID_INPUT, ToolError
    from .data import store
    st = studies.status(con, name)
    spec, folder = st["study"]["spec"], Path(st["study"]["folder"])
    if what != "deck" and not store.ready(con, name, what):
        todo = [i["message"] for i in store.issues(con, name, what) if i["severity"] in ("required", "decision")]
        raise ToolError(INVALID_INPUT, f"the {what} data isn't ready: " + ("; ".join(todo) if todo else
                        f"nothing loaded; the data manager runs `opmagent data load {name}`"))
    res = {t["task"]: t["result"] for t in st["tasks"]}
    src = {"study": name}
    from .data import deck_source
    if deck_source.has_deck(con, name):   # data read from a deck: rebuild it as it was
        if what in ("fluid", "geo", "res", "well"):
            geo_v = (res.get("geo") or {}).get("version_id") if what in ("well", "res") else None
            return deck_source.build_model(cfg, con, name, what, spec["models"][what], geo_version_id=geo_v,
                                           description=f"study {name}")
        if what == "deck":
            from .deck.build import build_deck
            missing = [k for k in MODEL_KINDS if not (res.get(k) or {}).get("version_id")]
            if missing:
                raise ToolError(INVALID_INPUT, f"no finished {', '.join(missing)} model in study {name} yet")
            ds = deck_source.deck_spec(con, name, spec["deck"], spec["requirement"],
                                       {k: int(res[k]["version_id"]) for k in MODEL_KINDS})
            (folder / "deck").mkdir(exist_ok=True)
            (folder / "deck" / "deck_spec.json").write_text(json.dumps(ds, indent=2, default=str))
            return build_deck(ds, cfg)
    if what == "fluid":
        from .fluid.pipeline import build_fluid_model
        from .servers.data_server import DataService
        from .servers.pvt_server import PVTService
        fi, gaps = store.fluid_input(con, name)
        return build_fluid_model(fi, spec["models"]["fluid"], data=DataService("fluid", cfg), pvt=PVTService(cfg),
                                 options={}, origin="built", user_description=f"study {name}", gaps=gaps)
    if what == "geo":
        from .geo.pipeline import build_geo_model
        gspec = store.geo_spec(con, name)
        return build_geo_model(cfg, tops=store.tops(con, name), logs=store.logs(con, name, gspec.get("curves")),
                               spec=gspec, name=spec["models"]["geo"], surveys=store.survey_xyz(con, name),
                               description=f"study {name}", data_source=src,
                               source_file_ids=store.source_file_ids(con, name, store.FOR["geo"]))
    if what == "res":
        from .res.pipeline import build_res_model
        geo_v = (res.get("geo") or {}).get("version_id")
        if not geo_v:
            raise ToolError(INVALID_INPUT, f"study {name} has no finished geo model yet: the res model is defined on it")
        return build_res_model(cfg, spec=store.res_spec(con, name), name=spec["models"].get("res", f"{name}_Res"),
                               geo_version_id=int(geo_v), description=f"study {name}", data_source=src,
                               source_file_ids=store.source_file_ids(con, name, store.FOR["res"]))
    if what == "well":
        from .well.pipeline import build_well_model
        geo_v = (res.get("geo") or {}).get("version_id")
        return build_well_model(cfg, store.well_inputs(con, name), spec["models"]["well"],
                                start_date=spec["start_date"], geo_version_id=geo_v,
                                unit_system=spec.get("unit_system", "METRIC"), description=f"study {name}",
                                data_source=src, source_file_ids=store.source_file_ids(con, name, store.FOR["well"]))
    if what == "deck":
        from .deck.build import build_deck
        missing = [k for k in MODEL_KINDS if not (res.get(k) or {}).get("version_id")]
        if missing:
            raise ToolError(INVALID_INPUT, f"no finished {', '.join(missing)} model in study {name} yet")
        ds = {"name": spec["deck"], "requirement": spec["requirement"], "start_date": spec["start_date"],
              "report": spec["report"], **{k: int(res[k]["version_id"]) for k in MODEL_KINDS}}
        for k in ("years", "end_date", "summary"):
            if spec.get(k):
                ds[k] = spec[k]
        (folder / "deck").mkdir(exist_ok=True)
        (folder / "deck" / "deck_spec.json").write_text(json.dumps(ds, indent=2))
        return build_deck(ds, cfg)
    raise ToolError(INVALID_INPUT, "build fluid, geo, res, well or deck")


def cmd_data(args):
    """Data manager: load a study's inbox into the database (load), only report (check), or show what is loaded."""
    import json
    from .core import db as dbmod
    from .core.errors import ToolError
    from .data import store
    from .data.load import load
    cfg = cfgmod.load(args.config)
    con = dbmod.connect(cfg.database)
    try:
        if args.dcmd == "from-deck":
            from .data.deck_source import load_deck
            m = load_deck(con, args.name, args.deck, write=not args.check)
            print(m["spoken_summary"])
            for kind, tabs in m["deck"]["tables"].items():
                print(f"  {kind:<6} " + ", ".join(f"{t} {n}" for t, n in tabs.items()))
            print(f"  coverage: {m['deck']['coverage']}  period {m['deck']['start_date']} to {m['deck']['end_date']}")
            for i in m["issues"]:
                print(f"    {i['modeler']:<5} [{i['severity']}] {i['item']}: {i['message']}"[:220])
            if args.json:
                print(json.dumps(m, indent=2, default=str))
            raise SystemExit(0 if m["all_ready"] else 2)
        if args.dcmd == "from-study":
            from .data.deck_source import copy_input
            m = copy_input(con, args.name, args.source, write=not args.check)
            print(m["spoken_summary"])
            if args.json:
                print(json.dumps(m, indent=2, default=str))
            raise SystemExit(0 if m["all_ready"] else 2)
        if args.dcmd == "verify":
            v = store.verify(con, args.name)
            print(v["spoken_summary"])
            for b in v["not_ok"]:
                print(f"  {b['status']:<8} {b['original']}  ({b['path']})")
            raise SystemExit(0 if not v["not_ok"] else 2)
        if args.dcmd == "show":
            m = store.inventory(con, args.name)
            if args.dataset == "deck":
                from .data.deck_source import deck_info
                d = deck_info(con, args.name)
                print(json.dumps({k: d[k] for k in ("source_path", "folder", "grid", "coverage", "spec")},
                                 indent=2, default=str)[:6000])
                return
            if args.dataset == "files":
                for f in store.files(con, args.name, well=args.well):
                    print(f"  {f['category']:<10} {f['format']:<10} {f['status']:<7} {f['original']}"
                          f"{'  (' + f['well_name'] + ')' if f['well_name'] else ''}")
                return
            if args.dataset:
                out = (store.log_stats(con, args.name, args.well) if args.dataset == "logs" else
                       store.rows(con, args.name, args.dataset, args.well, args.limit))
                print(json.dumps(out, indent=2, default=str))
                return
        else:
            m = load(con, args.name, write=args.dcmd == "load")
    except ToolError as e:
        raise SystemExit(str(e))
    print(m["spoken_summary"] + ("" if m.get("loaded", True) else "  (check only: nothing was written)"))
    for k, d in m["datasets"].items():
        if d["rows"] or args.dcmd != "show":
            extra = (f"  wells: {', '.join(d['wells'][:10])}" if d.get("wells") else "") + \
                    (f"  curves: {', '.join(d['curves'][:10])}" if d.get("curves") else "")
            print(f"  {k:<13} {d['rows']:>7} {d.get('unit', 'rows')}{extra}"[:200])
    for kind in MODEL_KINDS:
        print(f"  {kind}: {'ready' if m['ready'][kind] else 'NOT ready'}")
    for i in m["issues"]:
        print(f"    {i['modeler']:<5} [{i['severity']}] {i['item']}: {i['message']}"[:220])
    if m.get("unrecognised"):
        print(f"  not recognised (left in the inbox): {', '.join(m['unrecognised'])}")
    if args.json:
        print(json.dumps(m, indent=2, default=str))
    raise SystemExit(0 if m["all_ready"] else 2)


def cmd_results(args):
    """Results viewer: open a run in ResInsight (grid, properties over time, summary vectors)."""
    from .viewer import open_in_resinsight
    cfg = cfgmod.load(args.config)
    if args.init and args.deck is None:
        raise SystemExit("--init needs --deck <deck id>")
    r = open_in_resinsight(cfg, run_id=args.run, study=args.study, case=args.case, geo=args.geo,
                           deck=args.deck if args.init or args.deck is not None else None, dry_run=args.dry_run)
    print(r["spoken_summary"])
    if args.dry_run and r.get("command"):
        print(" ".join(r["command"]))
    raise SystemExit(0 if r["ok"] else 2)


def cmd_hm(args):
    """History matching: observations, misfit and the HM report on finished runs; setups, variants, sensitivities."""
    import json as _json

    from .core.errors import ToolError
    from .hm import service
    cfg = cfgmod.load(args.config)
    con = db.connect(cfg.database)
    if args.hcmd in HM_PHASE2:
        try:
            _cmd_hm2(args, cfg, con)
        except ToolError as e:
            raise SystemExit(f"{e.code}: {e.message}") from None
        return
    obj = _json.loads(Path(args.objective).read_text()) if getattr(args, "objective", None) else None
    kw = dict(study=args.study, source=args.source, truth_run_id=args.truth_run)
    try:
        if args.hcmd == "observations":
            r = service.observation_inventory(con, run_id=args.run, **kw)
            if args.json:
                print(_json.dumps(r, indent=2, default=str))
                return
            print(f"source {r['source']}, {r['period'][0]} to {r['period'][1]}, {r['points']} points")
            for w in r["wells"]:
                qs = ", ".join(f"{q} {d['non_zero']}/{d['points']}" for q, d in w["quantities"].items())
                ctl = f"  control {'/'.join(w['controls'])}, honoured {', '.join(w['honoured']) or '-'}" \
                    if w["controls"] else ""
                print(f"  {w['well']:<8} {qs}{ctl}")
            return
        if args.hcmd == "misfit":
            r = service.public(service.run_misfit(con, args.run, objective=obj, **kw), max_terms=args.terms)
            if args.json:
                print(_json.dumps(r, indent=2, default=str))
                return
            print(r["spoken_summary"])
            for t in r["terms"]:
                extra = (f"cum {t['cum_error_pct']:+.1f} %" if t.get("cum_error_pct") is not None else
                         f"last {t['last_error']:+.1f}" if "last_error" in t else "")
                flag = "" if t["in_total"] else "  (honoured, not in total)"
                print(f"  {t['well']:<8} {t['quantity']:<9} misfit {t['misfit']:10.3f}  n {t['n']:4d}  {extra}{flag}")
            return
        if args.hcmd == "report":
            r = service.write_report(cfg, con, args.run, objective=obj, **kw)
            print(r["spoken_summary"])
            print(r["report"])
            raise SystemExit(0 if r["report_ok"] else 2)
    except ToolError as e:
        raise SystemExit(f"{e.code}: {e.message}") from None


HM_PHASE2 = ("targets", "setup", "base", "sensitivity", "status", "stop", "cases", "case", "match-report",
             "optimise", "propose", "proposal", "confirm")


def _cmd_hm2(args, cfg, con):
    import json as _json

    from .hm import jobs, params
    out = lambda r: print(_json.dumps(r, indent=2, default=str))  # noqa: E731
    c = args.hcmd
    if c == "targets":
        t = params.targets(con, args.deck)
        if args.json:
            return out(t)
        print(f"grid {' x '.join(map(str, t['grid']))}")
        for z in t["zones"]:
            print(f"  zone {z['zone']:<12} layers {z['layers']:<7} PERMX {z['mean_permx']}")
        if not t["zones"]:     # an imported grid has no zones: parameters target layer ranges
            fmt = lambda v: "-" if v is None else f"{v:.4g}"  # noqa: E731
            print("  no zones: target layer ranges. Mean per layer:")
            for lay in t["layers"]:
                print(f"  layer {lay['layer']:<3} PERMX {fmt(lay['mean_permx']):>8}  PERMZ {fmt(lay['mean_permz']):>8}  "
                      f"PORO {fmt(lay['mean_poro']):>6}")
        for f in t["faults"]:
            print(f"  fault {f['fault']:<12} MULTFLT {f['multflt']:g}")
        for st in t["satnums"]:
            print(f"  SATNUM {st['satnum']}: " + ("tabulated" if st["tabulated"] else _json.dumps(st["corey"])))
        for a in t["aquifers"]:
            print(f"  aquifer {a['aquifer']} {a['type']} volume {a['water_volume']} PI {a['productivity_index']}")
        print("kinds: " + ", ".join(t["kinds"]))
        return
    if c == "setup":
        spec = _json.loads(Path(args.params).read_text())
        ps = spec["parameters"] if isinstance(spec, dict) else spec
        obj = spec.get("objective") if isinstance(spec, dict) else None
        r = jobs.setup(con, args.name, args.deck, ps, study=args.study, source=args.source,
                       truth_run_id=args.truth_run, objective=obj)
        print(r["read_back"])
        print(f"hm id {r['hm_id']}, revision {r['revision']}")
        return
    if c in ("base", "sensitivity"):
        h = jobs.get_hm(con, args.hm)
        job = jobs.create_job(con, h, c)
        print(f"job {job}: {len(jobs.plan(h, c))} case(s), {jobs.slots(cfg)} at a time")
        j = jobs.Runner(cfg, job, log=lambda m: print("  " + m)).run()
        print(jobs.spoken_job(j))
        if c == "sensitivity" and j.get("result"):
            for d in j["result"]["tornado"]:
                lo = "failed" if d["misfit_low"] is None else f"{d['misfit_low']:.4g}"
                hi = "failed" if d["misfit_high"] is None else f"{d['misfit_high']:.4g}"
                print(f"  {d['rank']:>2}. {d['name']:<16} low {lo:>10}  high {hi:>10}  "
                      f"best reduction {d['best_reduction']:.4g}")
        return
    if c == "status":
        j = jobs.get_job(con, args.job)
        return out(j) if args.json else print(jobs.spoken_job(j))
    if c == "stop":
        jobs.stop_job(con, args.job, args.now)
        print(f"job {args.job} will stop" + (" now" if args.now else " after its running cases"))
        return
    if c == "cases":
        h = jobs.get_hm(con, args.hm)
        rows = jobs.cases(con, h["id"], args.order, args.limit)
        if args.json:
            return out(rows)
        for r in rows:
            m = "-" if r["misfit"] is None else f"{r['misfit']:.4g}"
            vals = ", ".join(f"{k}={v:.4g}" for k, v in r["values"].items())
            print(f"  case {r['case_no']:>3} {r['status']:<7} misfit {m:>10}  {r['label'] or ''}  [{vals}]")
        return
    if c == "case":
        h = jobs.get_hm(con, args.hm)
        return out(jobs.case_detail(con, h["id"], args.case))
    if c == "optimise":
        h = jobs.get_hm(con, args.hm)
        job = jobs.create_job(con, h, "optimise", {"method": "lsq", "start": args.start,
                                                   "budget_runs": args.budget_runs, "budget_hours": args.budget_hours,
                                                   "target_misfit": args.target})
        print(f"job {job}: least squares, up to {args.budget_runs} case(s), {jobs.slots(cfg)} at a time")
        j = jobs.Runner(cfg, job, log=lambda m: print("  " + m)).run()
        print(jobs.spoken_job(j))
        r = j.get("result") or {}
        if r:
            print(f"  stopped because {r['stopped_because']} after {r['iterations']} iteration(s), "
                  f"{r['cases_run']} case(s); best case {r['best_case']} misfit {r['best_misfit']:.4g} "
                  f"(base {r['base_misfit'] if r['base_misfit'] is None else format(r['base_misfit'], '.4g')})")
            for k, v in r["best_values"].items():
                print(f"    {k} = {v:.6g}" + ("  (at a bound)" if k in r["at_bound"] else ""))
        return
    if c == "propose":
        from .hm import proposals
        r = proposals.propose(cfg, con, args.hm, case=args.case, note=args.note, digits=args.digits)
        print(r["spoken_summary"])
        return
    if c == "proposal":
        from .hm import proposals
        return out(proposals.show(con, args.proposal_id))
    if c == "confirm":
        from .hm import proposals
        r = proposals.confirm(con, args.proposal_id, args.run)
        print(r["spoken_summary"])
        raise SystemExit(0 if r["status"] == "confirmed" else 2)
    if c == "match-report":
        r = jobs.write_match_report(cfg, con, args.hm)
        print(r["spoken_summary"])
        print(r["report"])
        raise SystemExit(0 if r["report_ok"] else 2)


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


def cmd_backup(args):
    """A consistent copy of the database, the config and a manifest (and with --files the file store)."""
    from . import maintenance as mt
    cfg = cfgmod.load(args.config)
    r = mt.backup(cfg, config_path=cfgmod.find_path(args.config), out=args.out, files=args.files)
    m = r["manifest"]
    rows = ", ".join(f"{v} {k}" for k, v in m["rows"].items() if v)
    print(f"Backup: {r['archive']} ({mt.human(r['bytes'])})")
    print(f"  database {m['database']}{' and the file store' if m['with_files'] else ''}; {rows or 'empty'}")
    if not m["with_files"]:
        print(f"  the file store ({m['file_store']}) is not in it: add --files")


def cmd_clean(args):
    """List what can go (failed runs, older unapproved imports, views); remove it with --yes."""
    from . import maintenance as mt
    cfg = cfgmod.load(args.config)
    cats = [c for c in mt.CATEGORIES if getattr(args, c)] or list(mt.CATEGORIES)
    print(f"File store {cfg.file_store}: " + ", ".join(f"{u['folder']} {mt.human(u['bytes'])}" for u in mt.usage(cfg)))
    items = mt.plan(cfg, cats)
    if not items:
        print(f"Nothing to clean ({', '.join(cats)}).")
        return
    for c in cats:
        mine = [i for i in items if i["category"] == c]
        if mine:
            print(f"\n{c}: {len(mine)}, {mt.human(sum(i['bytes'] for i in mine))}")
            for i in mine:
                print(f"  {mt.human(i['bytes']):>9}  {i['path']}  ({i['why']})")
    total = sum(i["bytes"] for i in items)
    if not args.yes:
        print(f"\n{mt.human(total)} can go. Nothing removed: run again with --yes to remove it.")
        return
    r = mt.clean(cfg, items)
    print(f"\nRemoved {len(r['removed'])} folder(s), {mt.human(r['bytes'])} freed.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="path to opmagent.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the SQLite database and file store").set_defaults(fn=cmd_init_db)
    x = sub.add_parser("backup", help="back up the database (a consistent copy) and the config, into one archive")
    x.add_argument("--out", help="the folder for the archive (default: ~/.local/share/opmagent/backups)")
    x.add_argument("--files", action="store_true", help="also the file store (without the views: remade when opened)")
    x.set_defaults(fn=cmd_backup)
    x = sub.add_parser("clean", help="list (and with --yes remove) failed runs' files, older unapproved imports, views")
    x.add_argument("--runs", action="store_true", help="only the output folders of failed runs")
    x.add_argument("--imports", action="store_true", help="only older imports none of whose models are approved")
    x.add_argument("--views", action="store_true", help="only initialisation and static views (remade when opened)")
    x.add_argument("--yes", action="store_true", help="remove them (without it: only list them)")
    x.set_defaults(fn=cmd_clean)
    x = sub.add_parser("doctor", help="check prerequisites and the condition of each agent in the workspace")
    x.add_argument("--workspace", help="the agents workspace (default: the folder of the config, if setup made it)")
    x.add_argument("--deep", action="store_true", help="also start each MCP server over stdio and list its tools")
    x.add_argument("-v", "--verbose", action="store_true", help="every check, and each agent's tasks, requests, reviews, runs")
    x.add_argument("--json", action="store_true", help="the agents report as JSON")
    x.set_defaults(fn=cmd_doctor)
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
    x = fl.add_parser("qc", help="re-run the standard QC notebook for a draft version (prints the new report's file id)")
    x.add_argument("version_id", type=int)
    x.set_defaults(fn=cmd_model_notebook)
    x = fl.add_parser("investigate", help="start an investigation notebook for a fluid version")
    x.add_argument("version_id", type=int)
    x.add_argument("--purpose", required=True, help='what to look into, e.g. "C7+ MW sensitivity on the CVD match"')
    x.add_argument("--open", action="store_true", help="open it in VS Code ([debug] vscode_bin)")
    x.set_defaults(fn=cmd_model_notebook)
    x = fl.add_parser("run-notebook", help="execute an investigation notebook and export it to HTML")
    x.add_argument("path")
    x.set_defaults(fn=cmd_model_notebook)
    rv = sub.add_parser("review", help="human review (approve / reject)").add_subparsers(dest="rcmd", required=True)
    rv.add_parser("list", help="what is waiting for review").set_defaults(fn=cmd_review)
    for name in ("approve", "reject"):
        x = rv.add_parser(name)
        x.add_argument("kind", choices=list(MODEL_KINDS))
        x.add_argument("version_id", type=int)
        x.add_argument("--comment", required=name == "reject")
        x.set_defaults(fn=cmd_review)
    x = rv.add_parser("comment", help="comment on a model version; the modeler answers it in its next version")
    x.add_argument("kind", choices=list(MODEL_KINDS))
    x.add_argument("version_id", type=int)
    x.add_argument("text")
    x.add_argument("--item", help="what it is about, e.g. 'PERMX layer 3' or 'Psat'")
    x.add_argument("--severity", choices=["change", "question", "note"], default="change")
    x.set_defaults(fn=cmd_review)
    x = rv.add_parser("feedback", help="review comments and the modelers' answers (open ones unless --all)")
    x.add_argument("--kind", choices=list(MODEL_KINDS))
    x.add_argument("--model", help="model name")
    x.add_argument("--all", action="store_true")
    x.set_defaults(fn=cmd_review)
    x = rv.add_parser("approve-hello", help="approve the four hello demo models, then (run again) accept the hello "
                                            "deck's initial state")
    x.set_defaults(fn=cmd_review, comment=None)
    x = rv.add_parser("hm", help="a history-match proposal: show it, accept it (requests to the modelers) or reject it")
    x.add_argument("action", choices=["show", "accept", "reject"])
    x.add_argument("proposal_id", type=int)
    x.add_argument("--comment", help="why (required to reject)")
    x.set_defaults(fn=cmd_review)
    x = rv.add_parser("accept-init", help="accept a deck's initial state (the initialisation view) so it can run")
    x.add_argument("deck_id", type=int)
    x.add_argument("--comment")
    x.set_defaults(fn=cmd_review)
    x = rv.add_parser("reject-init", help="reject a deck's initial state; the comment goes to the model concerned")
    x.add_argument("deck_id", type=int)
    x.add_argument("--comment", required=True, help="what is wrong, e.g. 'oil below the OWC in EQLNUM 2'")
    x.add_argument("--to", default="res", choices=["fluid", "geo", "res", "well"],
                   help="the model the comment is for (default res: contacts, SCAL, regions)")
    x.set_defaults(fn=cmd_review)
    dk = sub.add_parser("deck", help="deck builder").add_subparsers(dest="dcmd", required=True)
    x = dk.add_parser("build", help="build a deck from a JSON spec")
    x.add_argument("--spec", required=True)
    x.set_defaults(fn=cmd_deck_build)
    x = dk.add_parser("init", help="initialisation view: the deck at time zero (initial state, fluids in place) for ResInsight")
    x.add_argument("deck_id", type=int)
    x.add_argument("--open", action="store_true", help="open it in ResInsight")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_deck_init)
    x = dk.add_parser("compare", help="are two decks the same simulation deck? (parsed: grid, tables, arrays, schedule)")
    x.add_argument("original")
    x.add_argument("generated")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_deck_compare)
    x = sub.add_parser("run", help="run a validated deck (shows live progress)")
    x.add_argument("--deck", required=True)
    x.set_defaults(fn=cmd_run)
    rn = sub.add_parser("runs", help="list runs, or follow one live").add_subparsers(dest="rcmd", required=True)
    rn.add_parser("list", help="runs started by the run agent").set_defaults(fn=cmd_runs)
    x = rn.add_parser("watch", help="follow a run: report step, date, %% done, elapsed, time left, stalls")
    x.add_argument("run", nargs="?", help="run id, a run folder or a run.log (default: the latest run)")
    x.add_argument("--interval", type=float, default=2.0, help="seconds between updates")
    x.add_argument("--once", action="store_true", help="print the current progress once and exit")
    x.set_defaults(fn=cmd_runs)
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
    x = wl.add_parser("qc", help="re-run the standard QC notebook for a draft version (prints the new report's file id)")
    x.add_argument("version_id", type=int)
    x.set_defaults(fn=cmd_model_notebook, nb_kind="well")
    x = wl.add_parser("investigate", help="start an investigation notebook for a well version")
    x.add_argument("version_id", type=int)
    x.add_argument("--purpose", required=True, help='what to look into, e.g. "P1 oil volume above measured"')
    x.add_argument("--open", action="store_true", help="open it in VS Code ([debug] vscode_bin)")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="well")
    x = wl.add_parser("run-notebook", help="execute an investigation notebook and export it to HTML")
    x.add_argument("path")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="well")
    sd = sub.add_parser("study", help="studies: the shared task board the agents work through").add_subparsers(
        dest="scmd", required=True)
    x = sd.add_parser("init", help="start a study: folder, study.json, task chain (deck builder)")
    x.add_argument("name")
    x.add_argument("--requirement", required=True, help="what the simulation should answer")
    x.add_argument("--years", type=int)
    x.add_argument("--end-date", dest="end_date")
    x.add_argument("--start-date", dest="start_date")
    x.add_argument("--report", choices=["monthly", "quarterly", "yearly"], default="monthly")
    x.add_argument("--unit-system", dest="unit_system", choices=["METRIC", "FIELD"], default="METRIC")
    x.add_argument("--summary", help="summary vectors, comma separated (default set if omitted)")
    x.set_defaults(fn=cmd_study)
    sd.add_parser("list", help="all studies").set_defaults(fn=cmd_study)
    x = sd.add_parser("status", help="tasks, open requests and what is next")
    x.add_argument("name")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("events", help="the study's history")
    x.add_argument("name")
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("compare", help="study from a deck: is the study's deck the source deck again (decks, then runs)?")
    x.add_argument("name")
    x.add_argument("--deck-only", dest="deck_only", action="store_true",
                   help="compare the two decks only (no simulation; needs the deck task, not the run)")
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("task", help="start or complete a study task from a terminal (agents use start_task/complete_task)")
    x.add_argument("name")
    x.add_argument("task", choices=["data", "fluid", "geo", "res", "well", "deck", "run", "view", "match"])
    x.add_argument("action", choices=["start", "complete"])
    x.add_argument("--result", help='the task result as JSON, e.g. \'{"version_id": 12}\'')
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("run", help="run the study's deck in Flow, store the results, complete the run task")
    x.add_argument("name")
    x.add_argument("--timeout", type=int, default=3600, help="seconds to wait for the run")
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("view", help="each model's QC report (plots of what it holds), before any simulation")
    x.add_argument("name")
    x.add_argument("kinds", nargs="*", choices=list(MODEL_KINDS), metavar="fluid|geo|res|well")
    x.add_argument("--open", action="store_true", help="open the reports in the browser")
    x.set_defaults(fn=cmd_study)
    x = sd.add_parser("build", help="build a model or the deck from the study's data in the database")
    x.add_argument("what", choices=["fluid", "geo", "res", "well", "deck"])
    x.add_argument("name")
    x.add_argument("--complete", action="store_true",
                   help="start the task first and complete it with the result (a model only once it is approved)")
    x.set_defaults(fn=cmd_study)
    da = sub.add_parser("data", help="data manager: load a study's raw data into the database").add_subparsers(
        dest="dcmd", required=True)
    for n_, h_ in (("check", "report what the inbox holds and what is missing (writes nothing)"),
                   ("load", "convert the study inbox into the study data tables for the modelers"),
                   ("show", "what is loaded; with a dataset, its rows (logs: per-curve statistics; files: catalog)"),
                   ("verify", "check the filed originals are still the files that were loaded"),
                   ("from-deck", "reverse: read an existing deck into the study's data, so the study rebuilds it"),
                   ("from-study", "take the input another study read from a deck (no new read of the deck)")):
        x = da.add_parser(n_, help=h_)
        x.add_argument("name")
        if n_ == "from-deck":
            x.add_argument("deck", help="the .DATA file (absolute, or relative to the study inbox)")
            x.add_argument("--check", action="store_true", help="report only, write nothing")
        if n_ == "from-study":
            x.add_argument("source", help="the study whose input to take")
            x.add_argument("--check", action="store_true", help="report only, write nothing")
        if n_ == "show":
            x.add_argument("dataset", nargs="?", help="wells, tops, surveys, perforations, controls, history, logs, "
                                                      "files, deck, sources, issues, log_curves, pvt_samples")
            x.add_argument("--well")
            x.add_argument("--limit", type=int, default=200)
        x.add_argument("--json", action="store_true")
        x.set_defaults(fn=cmd_data)
    rs = sub.add_parser("results", help="results viewer").add_subparsers(dest="vcmd", required=True)
    x = rs.add_parser("open", help="open a run in ResInsight")
    x.add_argument("--run", type=int, help="run id (default: the study's run, or the latest run)")
    x.add_argument("--study")
    x.add_argument("--case", help="an EGRID/.DATA path or a run folder")
    x.add_argument("--geo", type=int, help="a geo version id: its static view (grid and properties, no simulation)")
    x.add_argument("--deck", type=int, help="a deck id: its initialisation view (the dynamic model at time zero)")
    x.add_argument("--init", action="store_true", help="with --deck: the initialisation view (the default for --deck)")
    x.add_argument("--dry-run", action="store_true", help="print the command, don't start ResInsight")
    x.set_defaults(fn=cmd_results)
    gm = sub.add_parser("geo", help="geo modeler").add_subparsers(dest="gcmd", required=True)
    x = gm.add_parser("build", help="build a corner-point geo model from tops, logs and a spec")
    x.add_argument("--tops", required=True)
    x.add_argument("--logs", nargs="*", help="LAS or CSV log files")
    x.add_argument("--surveys", help="deviation surveys (to convert LAS MD to TVD)")
    x.add_argument("--spec", required=True, help="geo spec JSON (surfaces, layers, areal, contacts, petro, scal)")
    x.add_argument("--name", required=True)
    x.add_argument("--description")
    x.set_defaults(fn=cmd_geo_build)
    x = gm.add_parser("view", help="static view: the geo model as EGRID + INIT (OPM Flow dry run) for ResInsight")
    x.add_argument("version_id", type=int)
    x.add_argument("--res", type=int, help="include this res version's region arrays (default: the res on this geo)")
    x.add_argument("--no-res", action="store_true", help="the geo model alone")
    x.add_argument("--open", action="store_true", help="open it in ResInsight")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_geo_view)
    x = gm.add_parser("qc", help="re-run the standard QC notebook for a draft version (prints the new report's file id)")
    x.add_argument("version_id", type=int)
    x.set_defaults(fn=cmd_model_notebook, nb_kind="geo")
    x = gm.add_parser("investigate", help="start an investigation notebook for a geo version")
    x.add_argument("version_id", type=int)
    x.add_argument("--purpose", required=True, help='what to look into, e.g. "poor porosity fit at B in layer 2"')
    x.add_argument("--open", action="store_true", help="open it in VS Code ([debug] vscode_bin)")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="geo")
    x = gm.add_parser("run-notebook", help="execute an investigation notebook and export it to HTML")
    x.add_argument("path")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="geo")
    rs = sub.add_parser("res", help="res modeler (SCAL, rock, contacts, regions on a geo grid)").add_subparsers(
        dest="rscmd", required=True)
    x = rs.add_parser("build", help="build a res model on a geo version from a spec and submit it for review")
    x.add_argument("--spec", required=True, help="res spec JSON (scal, rock, contacts, regions)")
    x.add_argument("--geo-version", dest="geo_version", type=int, required=True, help="the geo version id it is defined on")
    x.add_argument("--name", required=True)
    x.add_argument("--description")
    x.add_argument("--scal", help="a SCAL workbook (opmagent res template): its rock types replace the spec's scal")
    x.set_defaults(fn=cmd_res_build)
    x = rs.add_parser("qc", help="re-run the standard QC notebook for a draft res version")
    x.add_argument("version_id", type=int)
    x.set_defaults(fn=cmd_model_notebook, nb_kind="res")
    x = rs.add_parser("investigate", help="start an investigation notebook for a res version")
    x.add_argument("version_id", type=int)
    x.add_argument("--purpose", required=True, help='what to look into, e.g. "water below the OWC in EQLNUM 2"')
    x.add_argument("--open", action="store_true", help="open it in VS Code ([debug] vscode_bin)")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="res")
    x = rs.add_parser("run-notebook", help="execute an investigation notebook and export it to HTML")
    x.add_argument("path")
    x.set_defaults(fn=cmd_model_notebook, nb_kind="res")
    x = rs.add_parser("template", help="write the SCAL workbook template (rock types: Corey or measured tables)")
    x.add_argument("out")
    x.add_argument("--example", action="store_true", help="fill in an example: one Corey and one measured rock type")
    x.set_defaults(fn=cmd_res_template)
    im = sub.add_parser("import", help="deck importer (reverse deck)").add_subparsers(dest="icmd", required=True)
    x = im.add_parser("deck", help="import a deck into fluid/geo/well drafts for review")
    x.add_argument("--input", required=True)
    x.add_argument("--name", required=True)
    x.set_defaults(fn=cmd_import)
    x = im.add_parser("verify", help="after approval: rebuild, run both, compare")
    x.add_argument("--name", required=True)
    x.set_defaults(fn=cmd_import)
    hm = sub.add_parser("hm", help="history matching: observations, misfit and the HM report (read only)") \
        .add_subparsers(dest="hcmd", required=True)
    for name, hlp in (("observations", "what can be matched: per well the quantities with data, the history control"),
                      ("misfit", "misfit of a finished run per well and quantity"),
                      ("report", "the HM report of a run (notebook, HTML)")):
        x = hm.add_parser(name, help=hlp)
        if name == "observations":
            x.add_argument("--run", type=int, help="a run: its deck's history and controls")
        else:
            x.add_argument("run", type=int)
            x.add_argument("--objective", help="JSON: weights, sigma, period, exclude_wells, include_honoured")
        x.add_argument("--study", help="the study whose measured production history is the observation")
        x.add_argument("--source", default="auto", choices=("auto", "data_production", "deck_history", "run"))
        x.add_argument("--truth-run", dest="truth_run", type=int, help="with --source run: the run taken as truth")
        if name != "report":
            x.add_argument("--json", action="store_true")
        if name == "misfit":
            x.add_argument("--terms", type=int, default=20, help="how many terms to list (worst first)")
        x.set_defaults(fn=cmd_hm)
    x = hm.add_parser("targets", help="what parameters can act on in a deck: zones, layers, faults, SATNUMs, aquifers")
    x.add_argument("--deck", type=int, required=True)
    x.add_argument("--json", action="store_true")
    x = hm.add_parser("setup", help="check and store a history-match setup (prints the read-back)")
    x.add_argument("name")
    x.add_argument("--deck", type=int, required=True)
    x.add_argument("--params", required=True, help='JSON: [parameters] or {"parameters": [...], "objective": {...}}')
    x.add_argument("--study")
    x.add_argument("--source", default="auto", choices=("auto", "data_production", "deck_history", "run"))
    x.add_argument("--truth-run", dest="truth_run", type=int)
    for name, hlp in (("base", "the base case's misfit (runs the base deck if it has no finished run)"),
                      ("sensitivity", "each parameter at its low and high end; prints the tornado")):
        x = hm.add_parser(name, help=hlp)
        x.add_argument("hm", help="history match name or id")
    x = hm.add_parser("status", help="a history-match job's progress or result")
    x.add_argument("job", type=int)
    x.add_argument("--json", action="store_true")
    x = hm.add_parser("stop", help="stop a job (continues where it stopped when started again)")
    x.add_argument("job", type=int)
    x.add_argument("--now", action="store_true", help="kill the running cases")
    x = hm.add_parser("cases", help="the evaluated cases, best first")
    x.add_argument("hm")
    x.add_argument("--order", default="misfit", choices=("misfit", "case"))
    x.add_argument("--limit", type=int, default=20)
    x.add_argument("--json", action="store_true")
    x = hm.add_parser("case", help="one case in detail (JSON)")
    x.add_argument("hm")
    x.add_argument("case", type=int)
    x = hm.add_parser("match-report", help="the HM report of a history match (best and base case, tornado)")
    x.add_argument("hm")
    x = hm.add_parser("optimise", help="bounded least squares from the best case so far (foreground, restartable)")
    x.add_argument("hm")
    x.add_argument("--start", default="best", choices=("best", "start"))
    x.add_argument("--budget-runs", dest="budget_runs", type=int, default=200)
    x.add_argument("--budget-hours", dest="budget_hours", type=float)
    x.add_argument("--target", type=float, help="stop at this misfit")
    x = hm.add_parser("propose", help="a match proposal from the best case (values rounded, run, for your review)")
    x.add_argument("hm")
    x.add_argument("--case", type=int)
    x.add_argument("--note")
    x.add_argument("--digits", type=int, default=3, help="significant figures of the proposed values")
    x = hm.add_parser("proposal", help="a match proposal (JSON)")
    x.add_argument("proposal_id", type=int)
    x = hm.add_parser("confirm", help="does a run of the rebuilt deck equal the proposal's case?")
    x.add_argument("proposal_id", type=int)
    x.add_argument("--run", type=int, required=True, help="the run of the deck built from the modelers' versions")
    for x in hm.choices.values():
        x.set_defaults(fn=cmd_hm)
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
