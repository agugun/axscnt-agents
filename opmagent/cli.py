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
    check("OPM Flow", lambda: shutil.which(flow) or (_ for _ in ()).throw(FileNotFoundError(f"{flow} not on PATH")))
    check("opm python", lambda: __import__("opm.io") and "importable")
    if cfg:
        check("database", lambda: "exists" if cfg.database.exists() else (_ for _ in ()).throw(FileNotFoundError("run: opmagent init-db")))
    raise SystemExit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="path to opmagent.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the SQLite database and file store").set_defaults(fn=cmd_init_db)
    sub.add_parser("doctor", help="check prerequisites").set_defaults(fn=cmd_doctor)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
