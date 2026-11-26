"""Review feedback: what the reviewer says about a model version, item by item, and the modeler's answer.

The reviewer (a person, through the reviewer profile or `opmagent review comment`) writes comments; a rejection's
comment is recorded here too. The modeler reads the open comments on its model (any version), builds a new
version that deals with them, and answers each one: addressed (with the version that addresses it) or wontfix
(with the reason). A comment itself can never be changed, only answered. Approvals stay in `approvals`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import models
from .errors import FORBIDDEN, INVALID_INPUT, NOT_FOUND, ToolError

SEVERITIES = ("change", "question", "note")
ANSWERS = ("addressed", "wontfix")


def add_comment(con, kind: str, version_id: int, comment: str, reviewer: str, item: str | None = None,
                severity: str = "change") -> dict:
    if severity not in SEVERITIES:
        raise ToolError(INVALID_INPUT, f"severity must be one of {SEVERITIES}")
    if not (comment or "").strip():
        raise ToolError(INVALID_INPUT, "a review comment needs text")
    h = models.header(con, kind, version_id)
    fid = con.execute("INSERT INTO review_feedback(model_kind, version_id, model_name, item, comment, severity, "
                      "reviewer) VALUES (?,?,?,?,?,?,?)",
                      (kind, version_id, h["model_name"], item, comment.strip(), severity, reviewer)).lastrowid
    return get(con, fid)


def get(con, feedback_id: int) -> dict:
    r = con.execute("SELECT f.*, NULL AS version FROM review_feedback f WHERE id = ?", (feedback_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no review comment {feedback_id}")
    d = dict(r)
    d["version"] = models.header(con, d["model_kind"], d["version_id"])["version"]
    d["anchor"], d["view"] = parse_item(d["item"])
    d["label"] = label(d["anchor"])
    return d


def list_feedback(con, kind: str | None = None, model_name: str | None = None, version_id: int | None = None,
                  status: str | None = "open") -> list[dict]:
    q, args = "SELECT id FROM review_feedback WHERE 1=1", []
    for col, v in (("model_kind", kind), ("model_name", model_name), ("version_id", version_id), ("status", status)):
        if v is not None:
            q, args = q + f" AND {col} = ?", args + [v]
    return [get(con, r[0]) for r in con.execute(q + " ORDER BY id", args)]


def answer(con, feedback_id: int, response: str, status: str = "addressed", addressed_in_version_id: int | None = None,
           kind: str | None = None) -> dict:
    """The modeler's answer. kind: the answering modeler's kind (it may answer only comments on its own models)."""
    f = get(con, feedback_id)
    if kind and f["model_kind"] != kind:
        raise ToolError(FORBIDDEN, f"comment {feedback_id} is on a {f['model_kind']} model")
    if status not in ANSWERS:
        raise ToolError(INVALID_INPUT, f"status must be one of {ANSWERS}")
    if not (response or "").strip():
        raise ToolError(INVALID_INPUT, "say what was done about the comment (or why not)")
    if f["status"] != "open":
        raise ToolError(INVALID_INPUT, f"comment {feedback_id} is already {f['status']}")
    if status == "addressed":
        if addressed_in_version_id is None:
            raise ToolError(INVALID_INPUT, "addressed: give the version_id that addresses it")
        h = models.header(con, f["model_kind"], addressed_in_version_id)
        if h["model_name"] != f["model_name"]:
            raise ToolError(INVALID_INPUT, f"version {addressed_in_version_id} is of {h['model_name']}, "
                                           f"not {f['model_name']}")
    con.execute("UPDATE review_feedback SET status = ?, response = ?, addressed_in_version_id = ?, answered_at = ? "
                "WHERE id = ?", (status, response.strip(), addressed_in_version_id,
                                 datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), feedback_id))
    return get(con, feedback_id)


def summary(items: list[dict]) -> str:
    if not items:
        return "No review comments."
    return f"{len(items)} comment(s): " + "; ".join(
        f"#{f['id']} {f['model_kind']} {f['model_name']} v{f['version']}"
        + (f" [{f['label']}]" if f["item"] else "") + f" {f['comment']}" for f in items[:5])


# ---------------------------------------------------------------- anchored items (the geo review workbench)
# item holds either free text (older comments, `opmagent review comment --item`) or JSON {"anchor": ..., "view": ...}:
# what the comment is about and the view it was made on (opmagent/geo/review_views.py).
def label(anchor: dict | None) -> str:
    """A short label for the anchor: 'W2', 'cell 4,7,5 PORO', 'TopRes at W3', 'layer 5'."""
    a = anchor or {}
    if not a:
        return "general"
    if "text" in a:
        return str(a["text"])
    bits = []
    if "finding" in a:
        bits.append(f"finding {a['finding']}")
    if "surface" in a:
        bits.append(a["surface"])
    if "cell" in a:
        bits.append("cell " + ",".join(map(str, a["cell"])))
    if "column" in a:
        bits.append("column " + ",".join(map(str, a["column"])))
    if "well" in a:
        bits.append(("at " if "surface" in a else "") + str(a["well"]))
    if "layer" in a:
        bits.append(f"layer {a['layer']}")
    if "zone" in a:
        bits.append(f"zone {a['zone']}")
    if "kw" in a:
        bits.append(a["kw"])
    return " ".join(bits)


def make_item(anchor: dict | None, view: dict | None) -> str:
    return json.dumps({"anchor": anchor or {}, **({"view": view} if view else {})}, separators=(",", ":"))


def parse_item(item: str | None) -> tuple[dict, dict | None]:
    """(anchor, view) from review_feedback.item; free text (or nothing) reads as {"text": ...} (or {})."""
    if not item:
        return {}, None
    try:
        d = json.loads(item)
    except ValueError:
        d = None
    if isinstance(d, dict) and "anchor" in d:
        return d.get("anchor") or {}, d.get("view")
    return {"text": item}, None
