"""Data MCP server. Start with a profile; it registers only that profile's tools.

    opmagent-data --profile fluid|geo|well|import|deck|run|data|view|reviewer [--config opmagent.toml]

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
from ..core import db, files, models, review, studies
from ..core.errors import FORBIDDEN, INVALID_INPUT, ToolError
from ..notebooks import runner

PROFILES = ("fluid", "geo", "well", "import", "deck", "run", "data", "view", "reviewer")
MODELER_KIND = {"fluid": "fluid", "geo": "geo", "well": "well"}
STUDY_TASK = {p: p for p in ("data", "fluid", "geo", "well", "deck", "run", "view")}   # profile -> its study task
AUTO_REVIEWER = "auto (approval disabled)"


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

    def submit_for_review(self, kind: str, version_id: int, qc_report_file_id: int, metrics: dict | None = None,
                          accept_validator_errors: bool = False) -> dict:
        """Re-run validation and, if clean, freeze the version as qc_ready for human review.
        accept_validator_errors: import profile only (data kept as it ran in OPM; findings go to the reviewer)."""
        self._kind_allowed(kind)
        if accept_validator_errors and self.profile != "import":
            raise ToolError(FORBIDDEN, "only the import profile may submit with validator findings")
        h = models.submit_for_review(self.con, kind, version_id, qc_report_file_id, metrics, accept_validator_errors)
        if self.cfg.auto_approve and not models.run_validator(self.con, kind, version_id)["errors"]:
            # [study] auto_approve: approval disabled; recorded as an approval by AUTO_REVIEWER, so it stays visible
            h = models.decide(self.con, kind, version_id, "approved", AUTO_REVIEWER, qc_report_file_id,
                              "auto-approved: [study] auto_approve = true and the validator passed")
            self._audit("auto_approve", {"kind": kind, "version_id": version_id}, "ok")
            return {"ok": True, "header": h, "auto_approved": True,
                    "spoken_summary": f"{kind} {h['model_name']} version {h['version']} passed its validator and was "
                                      f"approved automatically (approval is disabled)."}
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

    # ------------------------------------------------------------ studies (every agent: its own task only)
    def _my_task(self) -> str:
        t = STUDY_TASK.get(self.profile)
        if not t:
            raise ToolError(FORBIDDEN, f"profile '{self.profile}' has no study task")
        return t

    def init_study(self, name: str, requirement: str, years: int | None = None, end_date: str | None = None,
                   start_date: str | None = None, report: str = "monthly", unit_system: str = "METRIC",
                   summary: list[str] | None = None) -> dict:
        """Start a study (deck builder): creates its folder (inbox/, deck/, runs/), study.json and the
        task chain data -> fluid, geo, well -> deck -> run -> view. The data manager's task is ready first."""
        if self.profile != "deck":
            raise ToolError(FORBIDDEN, "only the deck builder starts studies")
        st = studies.init_study(self.con, self.cfg.studies_dir, name, requirement, actor=self.profile, years=years,
                                end_date=end_date, start_date=start_date, report=report, unit_system=unit_system,
                                summary=summary)
        st["spoken_summary"] = (f"Study {name} created in {st['study']['folder']}. Put its raw data in the inbox "
                                f"folder there; the data manager is next.")
        return {"ok": True, **st}

    def study_status(self, name: str) -> dict:
        """A study's tasks (waiting, ready, in_progress, blocked, done, failed), open requests, and what is next."""
        return {"ok": True, **studies.status(self.con, name)}

    def list_studies(self) -> dict:
        """All studies with how many of their tasks are done."""
        s = studies.list_studies(self.con)
        return {"ok": True, "studies": s, "spoken_summary": f"{len(s)} stud(ies)."}

    def start_task(self, name: str) -> dict:
        """Start your own task in a study (it must be ready)."""
        t = studies.start_task(self.con, name, self._my_task(), actor=self.profile)
        return {"ok": True, "task": t, "spoken_summary": f"Started {t['task']} in study {name}."}

    def complete_task(self, name: str, result: dict | None = None) -> dict:
        """Finish your task with its result: modelers {"version_id"} of the approved model, deck builder
        {"deck_id"}, run agent {"run_id"}, data manager {} once the data is loaded and ready (it records what was
        loaded). Unlocks the next tasks."""
        st = studies.complete_task(self.con, name, self._my_task(), result, actor=self.profile,
                                   auto_approve=self.cfg.auto_approve)
        nxt = ", ".join(f"{n} ({studies.AGENT[n]})" for n in st["unlocked"])
        st["spoken_summary"] = f"{self._my_task()} done in study {name}." + (f" Now ready: {nxt}." if nxt else "")
        return {"ok": True, **st}

    def fail_task(self, name: str, note: str) -> dict:
        """Mark your task failed with the reason (the coordinator can retry it)."""
        return {"ok": True, **studies.fail_task(self.con, name, self._my_task(), note, actor=self.profile)}

    def retry_task(self, name: str) -> dict:
        """Put your failed task back to ready (once what made it fail is fixed)."""
        return {"ok": True, **studies.retry_task(self.con, name, self._my_task(), actor=self.profile)}

    def raise_request(self, name: str, to_task: str, kind: str, message: str) -> dict:
        """Ask another task for something: kind missing_data, improve_data or question; to_task is usually 'data'.
        Your task is blocked until the request is resolved."""
        st = studies.raise_request(self.con, name, self._my_task(), to_task, kind, message, actor=self.profile)
        st["spoken_summary"] = (f"Request {st['request_id']} sent to {to_task} ({studies.AGENT[to_task]}); "
                                f"{self._my_task()} waits for it.")
        return {"ok": True, **st}

    def resolve_request(self, name: str, request_id: int, resolution: str) -> dict:
        """Close a request addressed to your task, saying what you did; the asking task becomes ready again."""
        st = studies.resolve_request(self.con, name, request_id, resolution, by_task=self._my_task(),
                                     actor=self.profile)
        st["spoken_summary"] = f"Request {request_id} resolved."
        return {"ok": True, **st}

    # ------------------------------------------------------------ study data (data manager writes, modelers read)
    def load_study_data(self, name: str, check_only: bool = False) -> dict:
        """Data manager: convert everything in the study's inbox into the study data tables (a reload replaces
        them) and check it with the modelers' readers. check_only=true reports without writing anything."""
        if self.profile != "data":
            raise ToolError(FORBIDDEN, "only the data manager loads study data")
        from ..data.load import load
        return {"ok": True, **load(self.con, name, write=not check_only)}

    def load_deck_data(self, name: str, deck_path: str, check_only: bool = False) -> dict:
        """Data manager, the reverse process: read an existing deck (absolute path, or relative to the study inbox)
        into the study's data, so the modelers rebuild exactly that deck: tables (PVT, SCAL, contacts, wells,
        completions, schedule) into the database, the grid and cell arrays as files. Replaces the study's data."""
        if self.profile != "data":
            raise ToolError(FORBIDDEN, "only the data manager loads study data")
        from ..data.deck_source import load_deck
        return {"ok": True, **load_deck(self.con, name, deck_path, write=not check_only)}

    def load_study_input(self, name: str, source_study: str, check_only: bool = False) -> dict:
        """Data manager: give study `name` the input another study read from a deck, without reading the deck again
        (tables row for row; the grid and array files copied after an integrity check). Replaces the study's data."""
        if self.profile != "data":
            raise ToolError(FORBIDDEN, "only the data manager loads study data")
        from ..data.deck_source import copy_input
        return {"ok": True, **copy_input(self.con, name, source_study, write=not check_only)}

    def deck_tables(self, name: str, kind: str | None = None, table: str | None = None, limit: int = 200) -> dict:
        """For a study whose data came from a deck: its tables per model (fluid pvto/pvdg/pvtw/density/...,
        geo swof/sgof/rock/contacts/faults, well wells/well_completions/well_events), first `limit` rows each."""
        from ..data.deck_source import deck_info, deck_tables
        info = deck_info(self.con, name)
        t = deck_tables(self.con, name, kind, table)
        n = {f"{k}.{tb}": len(r) for k, tabs in t.items() for tb, r in tabs.items()}
        t = {k: {tb: r[:int(limit)] for tb, r in tabs.items()} for k, tabs in t.items()}
        return {"ok": True, "source": info["source_path"], "grid": info["grid"], "props": info["props"],
                "spec": {k: v for k, v in info["spec"].items() if k not in ("passthrough", "summary_passthrough")},
                "row_counts": n, "tables": t, "spoken_summary": f"{sum(n.values())} row(s) in {len(n)} table(s)."}

    def study_data(self, name: str) -> dict:
        """What is loaded for a study: per dataset (pvt, tops, logs, geo_spec, contacts, wells, surveys,
        perforations, controls, history) its rows, wells and source files; the open data issues; who is ready."""
        from ..data import store
        return {"ok": True, **store.inventory(self.con, name)}

    def study_data_rows(self, name: str, dataset: str, well: str | None = None, limit: int = 200) -> dict:
        """Rows of one study dataset: wells, tops, surveys, perforations, controls, history, sources, issues,
        log_curves or pvt_samples, optionally for one well (for logs use log_stats / log_samples)."""
        from ..data import store
        r = store.rows(self.con, name, dataset, well, min(int(limit), 2000))
        return {"ok": True, "rows": r, "spoken_summary": f"{len(r)} {dataset} row(s)."}

    def log_stats(self, name: str, well: str | None = None) -> dict:
        """Per well and log curve, from the catalog (no file is opened): unit, samples, depth range,
        min / mean / max, and the LAS file it is in."""
        from ..data import store
        r = store.log_stats(self.con, name, well)
        return {"ok": True, "curves": r, "spoken_summary": f"{len(r)} curve(s) in "
                                                           f"{len({c['well_name'] for c in r})} well(s)."}

    def log_samples(self, name: str, well: str, curves: list[str] | None = None, top: float | None = None,
                    base: float | None = None, step: int = 1) -> dict:
        """One well's log samples (depth and a value per curve), read from its LAS file(s) after checking they are
        as loaded; optionally a depth window and every n-th sample. Keep it small: a window or a step."""
        from ..data import store
        r = store.log_samples(self.con, name, well, curves, top, base, step)
        if len(r) > 5000:
            raise ToolError(INVALID_INPUT, f"{len(r)} samples: narrow it with top/base or a larger step")
        return {"ok": True, "samples": r, "spoken_summary": f"{len(r)} sample(s) for {well}."}

    def study_files(self, name: str, category: str | None = None, well: str | None = None) -> dict:
        """The study's file catalog: data kept as files (logs, seismic, surfaces, grids, documents, images) and
        the filed originals of the tables, with format, path, original inbox name, well, status and metadata."""
        from ..data import store
        r = store.files(self.con, name, category, well)
        return {"ok": True, "files": r, "spoken_summary": f"{len(r)} file(s)."}

    def verify_study_files(self, name: str) -> dict:
        """Check every catalogued file is still the file that was loaded (changed or gone ones need a reload)."""
        from ..data import store
        return {"ok": True, **store.verify(self.con, name)}

    def check_study_data(self, name: str) -> dict:
        """Run the checks that combine tables and files: tops vs porosity logs, perforations vs surveys, OWC vs
        water saturation logs, production vs well types, files as loaded. Reports only; writes nothing."""
        from ..data.checks import run_checks
        out = [dict(zip(("modeler", "severity", "item", "message"), i)) for i in run_checks(self.con, name)]
        if self.profile in MODELER_KIND:
            out = [i for i in out if i["modeler"] == MODELER_KIND[self.profile]]
        return {"ok": True, "issues": out, "spoken_summary": f"{len(out)} finding(s)." if out else "All checks pass."}

    def fluid_sample(self, name: str) -> dict:
        """The selected PVT lab sample of the study as the fluid modeler reads it (composition, separator, CCE,
        DL, viscosity), with the data issues as gaps."""
        from ..data import store
        fi, gaps = store.fluid_input(self.con, name)
        return {"ok": True, "fluid_input": fi, "gaps": gaps,
                "spoken_summary": f"Sample {fi['sample_name']}, saturation pressure {fi['psat_lab']} bar, "
                                  f"{len(gaps)} gap(s)."}

    # ------------------------------------------------------------ review feedback
    def get_review_feedback(self, kind: str | None = None, model_name: str | None = None,
                            version_id: int | None = None, status: str | None = "open") -> dict:
        """Review comments on models (open ones by default; status null for all). A modeler sees its own kind;
        deal with the open ones in a new version and answer each with answer_review_feedback."""
        if self.profile in MODELER_KIND:
            if kind not in (None, MODELER_KIND[self.profile]):
                raise ToolError(FORBIDDEN, f"profile '{self.profile}' reads only {MODELER_KIND[self.profile]} feedback")
            kind = MODELER_KIND[self.profile]
        items = review.list_feedback(self.con, kind, model_name, version_id, status)
        return {"ok": True, "feedback": items, "spoken_summary": review.summary(items)}

    def answer_review_feedback(self, feedback_id: int, response: str, status: str = "addressed",
                               addressed_in_version_id: int | None = None) -> dict:
        """Answer a review comment on your model: status addressed (with the version_id that addresses it) or
        wontfix (with the reason). The comment itself can't be changed."""
        f = review.answer(self.con, feedback_id, response, status, addressed_in_version_id,
                          kind=MODELER_KIND.get(self.profile))
        return {"ok": True, "feedback": f, "spoken_summary": f"Comment {feedback_id} {status}."}

    def add_review_comment(self, kind: str, version_id: int, comment: str, item: str | None = None,
                           severity: str = "change") -> dict:
        """Reviewer: comment on a model version (severity change, question or note; item: what it is about,
        e.g. 'PERMX layer 3' or 'Psat'). The modeler answers it in the next version."""
        f = review.add_comment(self.con, kind, version_id, comment, self._reviewer(), item, severity)
        return {"ok": True, "feedback": f, "spoken_summary": f"Comment {f['id']} added to {kind} "
                                                             f"{f['model_name']} v{f['version']}."}

    # ------------------------------------------------------------ QC notebooks (fluid, geo and well modelers)
    def _qc_folder(self, version_id: int):
        kind = MODELER_KIND.get(self.profile)
        if kind not in runner.KINDS:
            raise ToolError(FORBIDDEN, f"profile '{self.profile}' has no QC notebooks")
        h = models.header(self.con, kind, version_id)
        return kind, h, runner.qc_dir(self.cfg.file_store, h["model_name"], h["version"], kind)

    def run_qc_notebook(self, version_id: int) -> dict:
        """Re-run the standard QC notebook (fluid, geo or well, by your profile) for a draft version (e.g. after
        return_to_draft) and register the new report. Pass the returned qc_report_file_id to submit_for_review.
        Versions under review or approved keep the report they were reviewed with."""
        kind, h, folder = self._qc_folder(version_id)
        if h["status"] != "draft":
            raise ToolError(FORBIDDEN, f"{kind} {h['model_name']} v{h['version']} is {h['status']}; its QC report is "
                                       "frozen (return it to draft first)")
        r = runner.run_qc(kind, folder, self.cfg.database, version_id, timeout=self.cfg.qc_timeout_s)
        if not r["ok"]:
            raise ToolError("QC_NOTEBOOK_FAILED", f"the QC notebook failed: {r['error']}", {"notebook": r["notebook"]})
        files.register_file(self.con, r["notebook"], "qc_report", f"executed QC notebook, {kind} version {version_id}")
        fid = files.register_file(self.con, r["html"], "qc_report")
        s = r["summary"]
        return {"ok": True, "qc_report_file_id": fid, "html": r["html"], "notebook": r["notebook"],
                "overall": s["overall"], "counts": s["counts"],
                "findings": [f for f in s["findings"] if f["status"] in ("fail", "warn")],
                "spoken_summary": f"QC notebook ran: {s['overall']}, {s['counts']['fail']} fail and "
                                  f"{s['counts']['warn']} warn finding(s)."}

    def create_investigation_notebook(self, version_id: int, purpose: str) -> dict:
        """Start a free-form investigation notebook for a fluid, geo or well version (any status): purpose, parameters and
        data-loading cells are written for you. Add analysis cells to the returned path (read only: load,
        compute, plot; model changes go through a new build), then call run_investigation_notebook."""
        kind, _, folder = self._qc_folder(version_id)
        path = runner.create_investigation(folder, self.cfg.database, version_id, purpose, kind)
        return {"ok": True, "path": str(path),
                "spoken_summary": f"Investigation notebook created: {path.name}. Add the analysis cells, then run it."}

    def run_investigation_notebook(self, path: str) -> dict:
        """Execute an investigation notebook (under the QC folder) in a fresh kernel, export it to HTML with the
        code shown, and register both as supporting evidence for the reviewer."""
        r = runner.run_notebook(path, self.cfg.file_store / "qc", timeout=self.cfg.qc_timeout_s)
        fid = files.register_file(self.con, r["html"], "qc_report", f"investigation: {os.path.basename(path)}")
        files.register_file(self.con, r["notebook"], "qc_report", f"investigation notebook: {os.path.basename(path)}")
        say = ("Investigation ran." if r["ok"] else
               f"Investigation stopped at a failing cell ({r['error']}); the outputs so far are saved.")
        return {"ok": True, "ran_clean": r["ok"], "error": r["error"], "html": r["html"], "notebook": r["notebook"],
                "file_id": fid, "spoken_summary": say}

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
        review.add_comment(self.con, kind, version_id, comment, self._reviewer(), "rejected", "change")
        return {"ok": True, "header": h, "spoken_summary": f"Rejected {kind} {h['model_name']} version {h['version']}."}

    def _reviewer(self) -> str:
        return os.environ.get("OPMAGENT_REVIEWER") or getpass.getuser()


# Which tools each profile sees
COMMON = ["resolve_name", "find_inbox_file", "list_models", "get_model"]
WRITER = ["register_file", "create_version", "update_draft", "write_tables", "run_validator", "submit_for_review",
          "return_to_draft"]
DATA_READ = ["study_data", "study_data_rows", "log_stats", "log_samples", "fluid_sample", "study_files",
             "verify_study_files", "check_study_data", "deck_tables"]
FEEDBACK = ["get_review_feedback", "answer_review_feedback"]
STUDY = ["study_status", "list_studies", "start_task", "complete_task", "fail_task", "retry_task", "raise_request",
         "resolve_request"]
QC_NOTEBOOKS = ["run_qc_notebook", "create_investigation_notebook", "run_investigation_notebook"]
TOOLS = {
    "fluid": COMMON + WRITER + STUDY + DATA_READ + FEEDBACK + QC_NOTEBOOKS, "geo": COMMON + WRITER + STUDY + DATA_READ + FEEDBACK + QC_NOTEBOOKS,
    "well": COMMON + WRITER + STUDY + DATA_READ + FEEDBACK + QC_NOTEBOOKS,
    "import": COMMON + WRITER,
    "deck": COMMON + ["create_deck", "add_deck_file", "set_deck_status", "list_decks", "get_deck", "init_study"] + STUDY,
    "run": COMMON + ["list_decks", "get_deck"] + STUDY,
    "data": COMMON + ["register_file", "load_study_data", "load_deck_data", "load_study_input"] + DATA_READ + STUDY,
    "view": COMMON + ["list_decks", "get_deck"] + STUDY,
    "reviewer": COMMON + ["list_pending_reviews", "get_qc_report", "approve", "reject", "add_review_comment",
                          "get_review_feedback", "list_decks", "get_deck", "study_status", "list_studies",
                          "study_data", "study_data_rows", "log_stats"],
}
WRITE_TOOLS = (set(WRITER) | {"create_deck", "add_deck_file", "set_deck_status", "approve", "reject", "init_study",
                               "load_study_data", "load_deck_data", "load_study_input", "answer_review_feedback", "add_review_comment"}
               | set(STUDY) | set(QC_NOTEBOOKS)) - {"run_validator", "study_status", "list_studies"}


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
