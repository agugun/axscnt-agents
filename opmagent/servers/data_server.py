"""Data MCP server. Start with a profile; it registers only that profile's tools.

    opmagent-data --profile fluid|geo|well|import|deck|run|reviewer [--config opmagent.toml]

Every tool returns a dict with "ok", a short "spoken_summary" and its result, or
{"ok": false, "code", "message", "details"} on failure. Write tools are recorded in audit_log.
"""
from __future__ import annotations

import argparse
import functools
import getpass
import hashlib
import json
import os
import threading

from mcp.server.mcpserver import MCPServer

from ..core import config as cfgmod
from ..core import db, files, models
from ..core.errors import FORBIDDEN, ToolError

PROFILES = ("fluid", "geo", "well", "import", "deck", "run", "reviewer")
MODELER_KIND = {"fluid": "fluid", "geo": "geo", "well": "well"}


class DataService:
    """Holds config, connection and profile; each public method becomes a tool."""

    def __init__(self, profile: str, cfg: cfgmod.Config):
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}")
        self.profile = profile
        self.cfg = cfg
        self.con = db.connect(cfg.database)
        self.lock = threading.Lock()  # MCP runs sync tools in worker threads

    # ------------------------------------------------------------ helpers
    def _audit(self, tool: str, args: dict, outcome: str):
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
        self.con.execute("INSERT INTO audit_log(tool, profile, args_sha256, outcome) VALUES (?,?,?,?)",
                         (tool, self.profile, digest, outcome))

    def _kind_allowed(self, kind: str):
        if self.profile == "import":
            return
        if MODELER_KIND.get(self.profile) != kind:
            raise ToolError(FORBIDDEN, f"profile '{self.profile}' cannot write {kind} models")

    def _origin(self, origin: str | None) -> str:
        if self.profile == "import":
            return "imported"
        if origin not in (None, "built", "demo"):
            raise ToolError(FORBIDDEN, "modelers may only create 'built' or 'demo' versions")
        return origin or "built"

    # ------------------------------------------------------------ shared
    def resolve_name(self, text: str, kind: str | None = None) -> dict:
        """Turn a spoken model reference ('field A oil version three', 'latest approved geo') into exact ids."""
        r = models.resolve_name(self.con, text, kind)
        if r["ambiguous"]:
            names = "; ".join(f"{c['kind']} {c['model_name']} v{c['version']}" for c in r["candidates"])
            return {"ok": True, **r, "spoken_summary": f"More than one match: {names}. Which one?"}
        m = r["match"]
        return {"ok": True, **r, "spoken_summary": f"That's {m['kind']} {m['model_name']} version {m['version']}, {m['status']}."}

    def find_inbox_file(self, query: str) -> dict:
        """Find input files in the inbox folder by spoken name, e.g. 'WELL-A1 PVT workbook'."""
        hits = files.find_in_inbox(self.cfg.inbox, query)
        say = (f"Found {hits[0].rsplit('/', 1)[-1]}." if len(hits) == 1 else
               f"Found {len(hits)} files; which one?" if hits else "No matching file in the inbox.")
        return {"ok": True, "files": hits, "spoken_summary": say}

    def list_models(self, kind: str | None = None, status: str | None = None) -> dict:
        """List model versions, optionally filtered by kind (fluid/geo/well) and status."""
        rows = models.list_models(self.con, kind, status)
        if self.profile in ("deck", "run"):
            rows = [r for r in rows if r["status"] == "approved"]
        return {"ok": True, "models": rows, "spoken_summary": f"{len(rows)} model version(s)."}

    def get_model(self, kind: str, version_id: int, include_tables: bool = True) -> dict:
        """Read one model version: header and all its tables."""
        if self.profile in MODELER_KIND and MODELER_KIND[self.profile] != kind:
            raise ToolError(FORBIDDEN, f"profile '{self.profile}' reads only {MODELER_KIND[self.profile]} models")
        m = models.get_model(self.con, kind, version_id, approved_only=self.profile in ("deck", "run"), tables=include_tables)
        h = m["header"]
        return {"ok": True, **m, "spoken_summary": f"{kind} {h['model_name']} version {h['version']}, {h['status']}."}

    # ------------------------------------------------------------ writers (fluid/geo/well/import)
    def register_file(self, path: str, kind: str, note: str | None = None) -> dict:
        """Record an input or output file (path + sha256). Returns its file id."""
        fid = files.register_file(self.con, path, kind, note)
        return {"ok": True, "file_id": fid, "spoken_summary": f"Registered file {fid}."}

    def create_version(self, kind: str, model_name: str, header: dict, origin: str | None = None,
                       description: str | None = None, source_file_ids: list[int] | None = None) -> dict:
        """Create a new draft version of a model (the model is created if new)."""
        self._kind_allowed(kind)
        if self.profile == "import" and not header.get("source_deck_file_id"):
            raise ToolError(FORBIDDEN, "imported versions need source_deck_file_id")
        r = models.create_version(self.con, kind, model_name, header, origin=self._origin(origin),
                                  description=description, source_file_ids=source_file_ids)
        return {"ok": True, **r, "spoken_summary": f"Created {kind} {model_name} version {r['version']} as a draft."}

    def update_draft(self, kind: str, version_id: int, fields: dict) -> dict:
        """Update descriptive fields of a draft (spec, assumptions, description, start date...)."""
        self._kind_allowed(kind)
        h = models.update_draft(self.con, kind, version_id, fields)
        return {"ok": True, "header": h, "spoken_summary": "Draft updated."}

    def write_tables(self, kind: str, version_id: int, table: str, rows: list[dict], replace: bool = False) -> dict:
        """Write rows into one of the model's tables (draft versions only)."""
        self._kind_allowed(kind)
        r = models.write_tables(self.con, kind, version_id, table, rows, replace=replace)
        return {"ok": True, **r, "spoken_summary": f"Wrote {r['written']} rows to {table}."}

    def run_validator(self, kind: str, version_id: int) -> dict:
        """Run the validator rules for a version without changing it."""
        r = models.run_validator(self.con, kind, version_id)
        say = (f"Validator passed with {len(r['warnings'])} warning(s)." if r["passed"] else
               f"Validator found {len(r['errors'])} error(s). First: {r['errors'][0]['message']}.")
        return {"ok": True, **r, "spoken_summary": say}

    def submit_for_review(self, kind: str, version_id: int, qc_report_file_id: int, metrics: dict | None = None) -> dict:
        """Re-run validation and, if clean, freeze the version as qc_ready for human review."""
        self._kind_allowed(kind)
        h = models.submit_for_review(self.con, kind, version_id, qc_report_file_id, metrics)
        return {"ok": True, "header": h,
                "spoken_summary": f"{kind} {h['model_name']} version {h['version']} is waiting for your review."}

    def return_to_draft(self, kind: str, version_id: int) -> dict:
        """Move a qc_ready version back to draft for revision (a new QC report will be needed)."""
        self._kind_allowed(kind)
        h = models.return_to_draft(self.con, kind, version_id)
        return {"ok": True, "header": h, "spoken_summary": "Back to draft."}

    # ------------------------------------------------------------ deck builder
    def create_deck(self, name: str, requirement_text: str, spec: dict, fluid_version_id: int,
                    geo_version_id: int, well_version_id: int, origin: str = "built") -> dict:
        """Register a deck built from approved fluid, geo and well versions."""
        d = models.create_deck(self.con, name, requirement_text, spec, fluid_version_id, geo_version_id,
                               well_version_id, origin)
        return {"ok": True, "deck": d, "spoken_summary": f"Deck {name} registered as number {d['id']}."}

    def add_deck_file(self, deck_id: int, path: str, role: str, kind: str = "include", main: bool = False) -> dict:
        """Register a deck file (main .DATA with main=true, or an include file)."""
        fid = files.register_file(self.con, path, "deck" if main else kind)
        d = models.add_deck_file(self.con, deck_id, fid, role, main)
        return {"ok": True, "deck": d, "spoken_summary": f"Added {role} to deck {deck_id}."}

    def set_deck_status(self, deck_id: int, status: str, validation_log: str | None = None) -> dict:
        """Set deck status: draft, validated, failed or ready."""
        d = models.set_deck_status(self.con, deck_id, status, validation_log)
        return {"ok": True, "deck": d, "spoken_summary": f"Deck {deck_id} is {status}."}

    def list_decks(self) -> dict:
        """List decks."""
        d = models.list_decks(self.con)
        return {"ok": True, "decks": d, "spoken_summary": f"{len(d)} deck(s)."}

    def get_deck(self, deck_id: int) -> dict:
        """Read a deck record with its files."""
        d = models.get_deck(self.con, deck_id)
        return {"ok": True, "deck": d, "spoken_summary": f"Deck {d['name']}, {d['status']}."}

    # ------------------------------------------------------------ reviewer (human only)
    def list_pending_reviews(self) -> dict:
        """List model versions waiting for review."""
        p = models.list_pending(self.con)
        say = ("Nothing is waiting for review." if not p else
               f"{len(p)} waiting: " + "; ".join(f"{r['kind']} {r['model_name']} version {r['version']}" for r in p) + ".")
        return {"ok": True, "pending": p, "spoken_summary": say}

    def get_qc_report(self, kind: str, version_id: int) -> dict:
        """Get a version's QC report file, validator warnings, metrics and assumptions."""
        r = models.get_qc_report(self.con, kind, version_id)
        qc = r["qc"] or {}
        nwarn = len(qc.get("validator", {}).get("warnings", []))
        metrics = qc.get("metrics", {})
        top = ", ".join(f"{k} {v}" for k, v in list(metrics.items())[:4])
        say = f"{kind} {r['model_name']} version {r['version']}: {nwarn} warning(s)" + (f"; {top}." if top else ".")
        return {"ok": True, **r, "spoken_summary": say}

    def approve(self, kind: str, version_id: int, qc_report_file_id: int, comment: str | None = None) -> dict:
        """Approve a qc_ready version. qc_report_file_id must be the report you reviewed."""
        h = models.decide(self.con, kind, version_id, "approved", self._reviewer(), qc_report_file_id, comment)
        return {"ok": True, "header": h, "spoken_summary": f"Approved {kind} {h['model_name']} version {h['version']}."}

    def reject(self, kind: str, version_id: int, qc_report_file_id: int, comment: str) -> dict:
        """Reject a qc_ready version with a comment explaining why."""
        h = models.decide(self.con, kind, version_id, "rejected", self._reviewer(), qc_report_file_id, comment)
        return {"ok": True, "header": h, "spoken_summary": f"Rejected {kind} {h['model_name']} version {h['version']}."}

    def _reviewer(self) -> str:
        return os.environ.get("OPMAGENT_REVIEWER") or getpass.getuser()


# Which tools each profile sees
COMMON = ["resolve_name", "find_inbox_file", "list_models", "get_model"]
WRITER = ["register_file", "create_version", "update_draft", "write_tables", "run_validator", "submit_for_review",
          "return_to_draft"]
TOOLS = {
    "fluid": COMMON + WRITER, "geo": COMMON + WRITER, "well": COMMON + WRITER, "import": COMMON + WRITER,
    "deck": COMMON + ["create_deck", "add_deck_file", "set_deck_status", "list_decks", "get_deck"],
    "run": COMMON + ["list_decks", "get_deck"],
    "reviewer": COMMON + ["list_pending_reviews", "get_qc_report", "approve", "reject", "list_decks", "get_deck"],
}
WRITE_TOOLS = set(WRITER) | {"create_deck", "add_deck_file", "set_deck_status", "approve", "reject"} - {"run_validator"}


def wrap(svc: DataService, name: str):
    fn = getattr(svc, name)

    @functools.wraps(fn)
    def tool(**kwargs):
        with svc.lock:
            return _call(**kwargs)

    def _call(**kwargs):
        try:
            out = fn(**kwargs)
            if name in WRITE_TOOLS:
                svc._audit(name, kwargs, "ok")
            return out
        except ToolError as e:
            if name in WRITE_TOOLS:
                svc._audit(name, kwargs, e.code)
            return e.to_dict()

    return tool


def build_server(profile: str, cfg: cfgmod.Config) -> tuple[MCPServer, DataService]:
    svc = DataService(profile, cfg)
    server = MCPServer(f"opmagent-data-{profile}", instructions=(
        "OPM agent data server. Large arrays go through files (register_file), never inline. "
        "Every result has a spoken_summary you can read aloud."))
    for name in TOOLS[profile]:
        server.add_tool(wrap(svc, name), name=name, description=getattr(DataService, name).__doc__)
    return server, svc


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent-data")
    p.add_argument("--profile", required=True, choices=PROFILES)
    p.add_argument("--config")
    a = p.parse_args(argv)
    server, _ = build_server(a.profile, cfgmod.load(a.config))
    server.run("stdio")


if __name__ == "__main__":
    main()
