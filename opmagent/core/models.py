"""Model, review and deck services: the logic behind the data server's tools.

Everything here takes an open sqlite3 connection and raises ToolError on failure,
so it can be tested without MCP.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3

from .. import validators
from .db import transaction
from .errors import AMBIGUOUS, DB_RULE, FORBIDDEN, INVALID_INPUT, NOT_FOUND, VALIDATION_FAILED, ToolError

KINDS = {
    "fluid": {
        "models": "fluid_models", "versions": "fluid_model_versions", "fk": "fluid_model_id",
        "sources": "fluid_version_sources",
        "tables": ["pvto", "pvdo", "pvdg", "pvtg", "pvtw", "density", "rsvd", "pbvd"],
    },
    "geo": {
        "models": "geo_models", "versions": "geo_model_versions", "fk": "geo_model_id",
        "sources": "geo_version_sources",
        "tables": ["geo_properties", "faults", "fault_multipliers"],
    },
    "res": {
        "models": "res_models", "versions": "res_model_versions", "fk": "res_model_id",
        "sources": "res_version_sources",
        "tables": ["res_properties", "swof", "sgof", "rock", "contacts", "aquifers", "aquifer_connections"],
    },
    "well": {
        "models": "well_models", "versions": "well_model_versions", "fk": "well_model_id",
        "sources": "well_version_sources",
        "tables": ["well_groups", "wells", "well_trajectory", "well_completions", "well_events",
                   "well_econ_limits", "group_controls", "vfp_tables"],
    },
}
# header columns a writer may never set directly
PROTECTED = {"id", "version", "status", "qc_json", "qc_report_file_id", "created_at", "qc_ready_at", "decided_at"}
DRAFT_EDITABLE = {"user_description", "spec_json", "assumptions_json", "start_date", "geo_version_id",
                  "minpv", "pinch_threshold"}


def _k(kind: str) -> dict:
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"unknown model kind '{kind}'", {"allowed": list(KINDS)})
    return KINDS[kind]


def _columns(con, table: str) -> list[str]:
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def _db(fn, *args):
    try:
        return fn(*args)
    except sqlite3.IntegrityError as e:
        raise ToolError(DB_RULE, str(e)) from e
    except sqlite3.OperationalError as e:
        raise ToolError(DB_RULE, str(e)) from e


def _jsonify(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else v


# ----------------------------------------------------------------- versions
def create_version(con, kind: str, model_name: str, header: dict, *, origin: str = "built",
                   description: str | None = None, source_file_ids: list[int] | None = None) -> dict:
    k = _k(kind)
    allowed = set(_columns(con, k["versions"])) - PROTECTED - {k["fk"]}
    bad = set(header) - allowed
    if bad:
        raise ToolError(INVALID_INPUT, f"unknown or protected header fields: {sorted(bad)}", {"allowed": sorted(allowed)})
    header = {kk: _jsonify(v) for kk, v in header.items()}
    header["origin"] = origin
    with transaction(con):
        row = con.execute(f"SELECT id FROM {k['models']} WHERE name = ?", (model_name,)).fetchone()
        model_id = row["id"] if row else _db(con.execute, f"INSERT INTO {k['models']}(name, description) VALUES (?,?)",
                                             (model_name, description)).lastrowid
        cols = [k["fk"], "version", *header]
        cur = _db(con.execute, f"INSERT INTO {k['versions']}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  (model_id, 0, *header.values()))
        vid = cur.lastrowid
        for fid in source_file_ids or []:
            _db(con.execute, f"INSERT INTO {k['sources']}(version_id, file_id) VALUES (?,?)", (vid, fid))
    v = con.execute(f"SELECT version FROM {k['versions']} WHERE id=?", (vid,)).fetchone()["version"]
    return {"kind": kind, "model_name": model_name, "version_id": vid, "version": v, "status": "draft", "origin": origin}


def update_draft(con, kind: str, version_id: int, fields: dict) -> dict:
    k = _k(kind)
    bad = set(fields) - DRAFT_EDITABLE
    if bad:
        raise ToolError(INVALID_INPUT, f"fields not editable: {sorted(bad)}", {"editable": sorted(DRAFT_EDITABLE)})
    present = set(_columns(con, k["versions"]))
    fields = {kk: _jsonify(v) for kk, v in fields.items() if kk in present}
    _require_status(con, kind, version_id, {"draft"})
    if fields:
        sets = ", ".join(f"{c} = ?" for c in fields)
        _db(con.execute, f"UPDATE {k['versions']} SET {sets} WHERE id = ?", (*fields.values(), version_id))
    return header(con, kind, version_id)


def write_tables(con, kind: str, version_id: int, table: str, rows: list[dict], *, replace: bool = False) -> dict:
    k = _k(kind)
    if table not in k["tables"]:
        raise ToolError(INVALID_INPUT, f"table '{table}' does not belong to a {kind} model", {"tables": k["tables"]})
    _require_status(con, kind, version_id, {"draft"})
    cols = [c for c in _columns(con, table) if c not in ("version_id", "id")]
    for i, r in enumerate(rows):
        bad = set(r) - set(cols)
        if bad:
            raise ToolError(INVALID_INPUT, f"row {i}: unknown columns {sorted(bad)}", {"columns": cols})
    with transaction(con):
        if replace:
            _db(con.execute, f"DELETE FROM {table} WHERE version_id = ?", (version_id,))
        for i, r in enumerate(rows):
            names = ["version_id", *r]
            try:
                con.execute(f"INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                            (version_id, *r.values()))
            except sqlite3.DatabaseError as e:
                raise ToolError(DB_RULE, f"{table} row {i}: {e}", {"row": r}) from e
    n = con.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE version_id=?", (version_id,)).fetchone()["n"]
    return {"table": table, "written": len(rows), "rows_now": n}


def header(con, kind: str, version_id: int) -> dict:
    k = _k(kind)
    row = con.execute(
        f"SELECT v.*, m.name AS model_name FROM {k['versions']} v JOIN {k['models']} m ON m.id = v.{k['fk']} WHERE v.id = ?",
        (version_id,)).fetchone()
    if not row:
        raise ToolError(NOT_FOUND, f"{kind} version {version_id} not found")
    d = dict(row)
    for j in ("spec_json", "assumptions_json", "qc_json"):
        if d.get(j):
            try:
                d[j] = json.loads(d[j])
            except ValueError:
                pass
    return d


def get_model(con, kind: str, version_id: int, *, approved_only: bool = False, tables: bool = True) -> dict:
    h = header(con, kind, version_id)
    if approved_only and h["status"] != "approved":
        raise ToolError(FORBIDDEN, f"{kind} {h['model_name']} v{h['version']} is {h['status']}, not approved")
    out = {"header": h}
    if tables:
        out["tables"] = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t} WHERE version_id=?", (version_id,))]
                         for t in _k(kind)["tables"]}
    return out


def list_models(con, kind: str | None = None, status: str | None = None) -> list[dict]:
    out = []
    for kd in ([kind] if kind else KINDS):
        k = _k(kd)
        sql = (f"SELECT v.id AS version_id, m.name AS model_name, v.version, v.status, v.origin, v.unit_system, v.created_at "
               f"FROM {k['versions']} v JOIN {k['models']} m ON m.id = v.{k['fk']}")
        args = ()
        if status:
            sql += " WHERE v.status = ?"
            args = (status,)
        out += [{"kind": kd, **dict(r)} for r in con.execute(sql + " ORDER BY m.name, v.version", args)]
    return out


def _require_status(con, kind, version_id, allowed: set[str]):
    h = header(con, kind, version_id)
    if h["status"] not in allowed:
        raise ToolError(FORBIDDEN, f"{kind} {h['model_name']} v{h['version']} is {h['status']}; needs {sorted(allowed)}")
    return h


# ----------------------------------------------------------------- validation and review
def run_validator(con, kind: str, version_id: int) -> dict:
    header(con, kind, version_id)
    issues = validators.run(kind, con, version_id)
    errors, warnings = validators.split(issues)
    return {"passed": not errors, "errors": errors, "warnings": warnings}


def submit_for_review(con, kind: str, version_id: int, qc_report_file_id: int, metrics: dict | None = None,
                      accept_validator_errors: bool = False) -> dict:
    """Re-run the validator server-side; only a clean result moves the version to qc_ready.
    accept_validator_errors is for imported models only: the data is stored as it ran in OPM and the
    validator findings go to the reviewer in qc_json instead of blocking."""
    k = _k(kind)
    h = _require_status(con, kind, version_id, {"draft"})
    result = run_validator(con, kind, version_id)
    if not result["passed"] and not (accept_validator_errors and h["origin"] == "imported"):
        raise ToolError(VALIDATION_FAILED, f"{len(result['errors'])} validator error(s); fix and resubmit", result)
    qc = {"validator": {"errors": len(result["errors"]), "error_list": result["errors"], "warnings": result["warnings"],
                        "accepted_as_imported": bool(result["errors"])}, "metrics": metrics or {}}
    with transaction(con):
        _db(con.execute, f"UPDATE {k['versions']} SET qc_json = ?, qc_report_file_id = ? WHERE id = ?",
            (json.dumps(qc), qc_report_file_id, version_id))
        _db(con.execute, f"UPDATE {k['versions']} SET status = 'qc_ready' WHERE id = ?", (version_id,))
    return header(con, kind, version_id)


def return_to_draft(con, kind: str, version_id: int) -> dict:
    k = _k(kind)
    _require_status(con, kind, version_id, {"qc_ready"})
    _db(con.execute, f"UPDATE {k['versions']} SET status = 'draft' WHERE id = ?", (version_id,))
    return header(con, kind, version_id)


def list_pending(con) -> list[dict]:
    return list_models(con, status="qc_ready")


def get_qc_report(con, kind: str, version_id: int) -> dict:
    h = header(con, kind, version_id)
    f = None
    if h["qc_report_file_id"]:
        r = con.execute("SELECT id, path FROM files WHERE id=?", (h["qc_report_file_id"],)).fetchone()
        f = dict(r) if r else None
    return {"kind": kind, "model_name": h["model_name"], "version": h["version"], "status": h["status"],
            "qc_ready_at": h["qc_ready_at"], "qc_report": f, "qc": h["qc_json"], "assumptions": h["assumptions_json"]}


def decide(con, kind: str, version_id: int, decision: str, reviewer: str, qc_report_file_id: int,
           comment: str | None = None) -> dict:
    if decision not in ("approved", "rejected"):
        raise ToolError(INVALID_INPUT, "decision must be 'approved' or 'rejected'")
    if decision == "rejected" and not (comment or "").strip():
        raise ToolError(INVALID_INPUT, "a rejection needs a comment")
    _require_status(con, kind, version_id, {"qc_ready"})
    _db(con.execute, "INSERT INTO approvals(model_kind, version_id, decision, reviewer, comment, qc_report_file_id) "
                     "VALUES (?,?,?,?,?,?)", (kind, version_id, decision, reviewer, comment, qc_report_file_id))
    return header(con, kind, version_id)


# ----------------------------------------------------------------- spoken names
_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                   "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_KIND_WORDS = {"fluid": "fluid", "pvt": "fluid", "oil": None, "geo": "geo", "grid": "geo", "geological": "geo",
               "well": "well", "wells": "well", "res": "res", "reservoir": "res", "scal": "res"}


def resolve_name(con, text: str, kind: str | None = None) -> dict:
    """Turn a spoken reference ('field A oil version three', 'latest approved geo') into version ids."""
    t = text.lower()
    m = re.search(r"\b(?:version|v)\s*(\d+|" + "|".join(_NUM) + r")\b", t)
    version = None
    if m:
        version = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        t = t[:m.start()] + t[m.end():]
    latest_approved = "latest approved" in t or "approved" in t and version is None
    latest = "latest" in t
    if kind is None:
        for w, kd in _KIND_WORDS.items():
            if kd and re.search(rf"\b{w}\b", t):
                kind = kd
                break
    words = [w for w in re.split(r"[\s_\-]+", re.sub(r"\b(latest|approved|the|model|version|deck|of|for)\b", " ", t)) if w]
    candidates = []
    for row in list_models(con, kind):
        name = row["model_name"].lower()
        compact = re.sub(r"[\s_\-]+", "", name)
        joined = "".join(words)
        score = max(difflib.SequenceMatcher(None, joined, compact).ratio(),
                    sum(w in name for w in words) / max(len(words), 1))
        if score >= 0.5:
            candidates.append({**row, "score": round(score, 2)})
    if version is not None:
        candidates = [c for c in candidates if c["version"] == version]
    elif latest_approved:
        candidates = [c for c in candidates if c["status"] == "approved"]
        candidates = _latest_per_model(candidates)
    elif latest:
        candidates = _latest_per_model(candidates)
    candidates.sort(key=lambda c: (-c["score"], c["model_name"], -c["version"]))
    if not candidates:
        raise ToolError(NOT_FOUND, f"no model matches '{text}'")
    top = [c for c in candidates if c["score"] == candidates[0]["score"]]
    if len(top) > 1 and len({(c["kind"], c["version_id"]) for c in top}) > 1:
        return {"match": None, "candidates": top, "ambiguous": True}
    return {"match": candidates[0], "candidates": candidates[:5], "ambiguous": False}


def _latest_per_model(cands):
    best = {}
    for c in cands:
        key = (c["kind"], c["model_name"])
        if key not in best or c["version"] > best[key]["version"]:
            best[key] = c
    return list(best.values())


# ----------------------------------------------------------------- decks
def create_deck(con, name: str, requirement_text: str, spec: dict, fluid_version_id: int, geo_version_id: int,
                well_version_id: int, res_version_id: int, origin: str = "built") -> dict:
    cur = _db(con.execute,
              "INSERT INTO decks(name, requirement_text, spec_json, fluid_version_id, geo_version_id, well_version_id, "
              "res_version_id, origin) VALUES (?,?,?,?,?,?,?,?)",
              (name, requirement_text, json.dumps(spec), fluid_version_id, geo_version_id, well_version_id,
               res_version_id, origin))
    return get_deck(con, cur.lastrowid)


def add_deck_file(con, deck_id: int, file_id: int, role: str, main: bool = False) -> dict:
    _db(con.execute, "INSERT OR REPLACE INTO deck_files(deck_id, file_id, role) VALUES (?,?,?)", (deck_id, file_id, role))
    if main:
        _db(con.execute, "UPDATE decks SET deck_file_id = ? WHERE id = ?", (file_id, deck_id))
    return get_deck(con, deck_id)


def set_deck_status(con, deck_id: int, status: str, validation_log: str | None = None) -> dict:
    _db(con.execute, "UPDATE decks SET status = ?, validation_log = COALESCE(?, validation_log) WHERE id = ?",
        (status, validation_log, deck_id))
    return get_deck(con, deck_id)


def get_deck(con, deck_id: int) -> dict:
    r = con.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"deck {deck_id} not found")
    d = dict(r)
    d["spec_json"] = json.loads(d["spec_json"])
    d["files"] = [dict(x) for x in con.execute(
        "SELECT f.id, f.path, df.role FROM deck_files df JOIN files f ON f.id = df.file_id WHERE df.deck_id = ?", (deck_id,))]
    return d


def list_decks(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT id, name, status, origin, fluid_version_id, geo_version_id, "
                                         "res_version_id, well_version_id, created_at FROM decks ORDER BY id")]


def ambiguous(result: dict) -> None:
    if result.get("ambiguous"):
        raise ToolError(AMBIGUOUS, "more than one model matches; please choose", result["candidates"])


def review_state(submit_result: dict) -> str:
    """How a pipeline's summary describes a submitted version: waiting for review, or approved automatically."""
    if submit_result.get("auto_approved"):
        return "passed its validator and was approved automatically (approval is disabled)"
    return "is waiting for your review"

