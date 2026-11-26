"""Model proposals (geo review workbench, phase 2): a modeler's change to a version's spec, agreed before it is built.

  propose     the modeler: a JSON merge patch on the base version's spec, the threads it answers, why, and what it
              should change. One open proposal per base version: a new one supersedes a proposed one; an accepted one
              must be built first. Posted as a message in each thread.
  decide      the engineer only (workbench or `opmagent review proposal`): accept or reject (with a comment). Posted
              in the threads, so they wait on the modeler again.
  mark_built  the build from an accepted proposal (`--from-proposal`) records its version and posts it in the threads.
              The modeler then answers each thread with that version.

Kind-neutral table (model_proposals): geo and res. A well model's changes are data edits (the data manager's), so
well has no proposals.
"""
from __future__ import annotations

import copy
import json

from . import models, review
from .errors import FORBIDDEN, INVALID_INPUT, NOT_FOUND, ToolError

KINDS = ("geo", "res")
PATCHABLE = {"geo": ("layers", "areal", "petro", "curves", "contacts", "min_thickness"),
             "res": ("scal", "rock", "contacts", "regions", "aquifers")}
SPEC_KEY = {"geo": "geo_spec", "res": "res_spec"}
OPEN = ("proposed", "accepted")


def merge_patch(base, patch):
    """RFC 7386 JSON merge patch: objects merge key by key, null removes a key, anything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


ID_KEYS = ("satnum", "eqlnum", "region", "id")


def flatten(d, prefix="") -> dict:
    """Dotted keys to leaf values. A list of objects (SCAL rock types, contacts per EQLNUM, rock per region) is keyed
    by its satnum / eqlnum / region / id, else its place (1-based), so a change reads as "scal.2.nw 3 -> 2.5"."""
    out = {}
    if isinstance(d, dict) and d:
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(d, list) and d and all(isinstance(x, dict) for x in d):
        for n, x in enumerate(d, start=1):
            key = next((x[k] for k in ID_KEYS if k in x), n)
            out.update(flatten({kk: vv for kk, vv in x.items() if kk not in ID_KEYS[:3]} or {"": None},
                               f"{prefix}.{key}"))
    else:
        out[prefix] = d
    return out


def spec_diff(old: dict, new: dict) -> list[dict]:
    """[{key, old, new}] for every dotted key that differs."""
    a, b = flatten(old or {}), flatten(new or {})
    return [{"key": k, "old": a.get(k), "new": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def base_spec(con, kind: str, version_id: int) -> dict:
    h = models.header(con, kind, version_id)
    sj = h.get("spec_json")
    sj = json.loads(sj) if isinstance(sj, str) else (sj or {})
    return sj.get(SPEC_KEY[kind]) or {}


def check_spec(con, kind: str, base_version_id: int, spec: dict):
    """The merged spec must be one the build accepts."""
    if kind == "geo":
        _check_geo_spec(spec)
    else:
        from ..res.pipeline import resolve_spec
        h = models.header(con, "res", base_version_id)
        resolve_spec(spec, models.header(con, "geo", h["geo_version_id"]))


def _check_geo_spec(spec: dict):
    s, lay = spec.get("surfaces") or [], spec.get("layers")
    if not isinstance(lay, list) or len(lay) != len(s) - 1 or not all(isinstance(n, int) and n >= 1 for n in lay):
        raise ToolError(INVALID_INPUT, f"layers must be {len(s) - 1} positive integers, one per zone ({s})")
    cut = (spec.get("petro") or {}).get("vsh_cutoff")
    if cut is not None and not (isinstance(cut, (int, float)) and 0 < cut <= 1):
        raise ToolError(INVALID_INPUT, "petro.vsh_cutoff must be in (0, 1]")
    kvkh = (spec.get("petro") or {}).get("kv_kh")
    if kvkh is not None and not (isinstance(kvkh, (int, float)) and 0 < kvkh <= 1):
        raise ToolError(INVALID_INPUT, "petro.kv_kh must be in (0, 1]")


def get(con, proposal_id: int) -> dict:
    r = con.execute("SELECT * FROM model_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no proposal {proposal_id}")
    p = dict(r)
    for k in ("feedback_ids", "spec_patch", "expected", "preview"):
        v = p.pop(f"{k}_json")
        p[k] = json.loads(v) if v else None
    base = base_spec(con, p["model_kind"], p["base_version_id"])
    p["spec"] = merge_patch(base, p["spec_patch"])
    p["changes"] = spec_diff(base, p["spec"])
    p["base_version"] = models.header(con, p["model_kind"], p["base_version_id"])["version"]
    if p["built_version_id"]:
        p["built_version"] = models.header(con, p["model_kind"], p["built_version_id"])["version"]
    return p


def list_proposals(con, kind: str = "geo", model_name: str | None = None, status: str | None = None,
                   base_version_id: int | None = None) -> list[dict]:
    q, args = "SELECT id FROM model_proposals WHERE model_kind = ?", [kind]
    for col, v in (("model_name", model_name), ("status", status), ("base_version_id", base_version_id)):
        if v is not None:
            q, args = q + f" AND {col} = ?", args + [v]
    return [get(con, r[0]) for r in con.execute(q + " ORDER BY id", args)]


def describe_changes(changes: list[dict]) -> str:
    return "; ".join(f"{c['key']} {json.dumps(c['old'])} -> {json.dumps(c['new'])}" for c in changes) or "no change"


def propose(con, kind: str, base_version_id: int, feedback_ids: list[int], spec_patch: dict, rationale: str,
            author: str, author_role: str = "modeler", expected: dict | list | str | None = None,
            preview: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"proposals are for {KINDS} models for now")
    h = models.header(con, kind, base_version_id)
    if not isinstance(spec_patch, dict) or not spec_patch:
        raise ToolError(INVALID_INPUT, "spec_patch must be a non-empty JSON object (a merge patch on the spec)")
    bad = set(spec_patch) - set(PATCHABLE[kind])
    if bad:
        raise ToolError(INVALID_INPUT, f"spec_patch can change {PATCHABLE[kind]}, not {sorted(bad)}"
                                       + (" (other surfaces need other tops: a data request)" if kind == "geo" else ""))
    if not (rationale or "").strip():
        raise ToolError(INVALID_INPUT, "say why (rationale)")
    ids = sorted({int(i) for i in (feedback_ids or [])})
    if not ids:
        raise ToolError(INVALID_INPUT, "a proposal answers at least one review comment (feedback_ids)")
    for fid in ids:
        f = review.get(con, fid)
        if f["model_kind"] != kind or f["model_name"] != h["model_name"]:
            raise ToolError(INVALID_INPUT, f"comment {fid} is not on {kind} {h['model_name']}")
        if f["status"] != "open":
            raise ToolError(INVALID_INPUT, f"comment {fid} is {f['status']}")
    base = base_spec(con, kind, base_version_id)
    if not base:
        raise ToolError(INVALID_INPUT, f"{kind} version {base_version_id} has no spec to patch (a version from a deck)")
    spec = merge_patch(base, spec_patch)
    check_spec(con, kind, base_version_id, spec)
    changes = spec_diff(base, spec)
    if not changes:
        raise ToolError(INVALID_INPUT, "the patch changes nothing in the base spec")
    acc = con.execute("SELECT id FROM model_proposals WHERE model_kind = ? AND base_version_id = ? AND status = "
                      "'accepted'", (kind, base_version_id)).fetchone()
    if acc:
        raise ToolError(INVALID_INPUT, f"proposal {acc[0]} on this version is accepted: build it first")
    old = [r[0] for r in con.execute("SELECT id FROM model_proposals WHERE model_kind = ? AND base_version_id = ? "
                                     "AND status = 'proposed'", (kind, base_version_id))]
    con.execute("UPDATE model_proposals SET status = 'superseded' WHERE id IN (%s)" % ",".join("?" * len(old)), old) \
        if old else None
    pid = con.execute(
        "INSERT INTO model_proposals(model_kind, model_name, base_version_id, feedback_ids_json, spec_patch_json, "
        "rationale, expected_json, preview_json, author_role, author) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, h["model_name"], base_version_id, json.dumps(ids), json.dumps(spec_patch), rationale.strip(),
         json.dumps(expected) if expected is not None else None, json.dumps(preview) if preview else None,
         author_role, author)).lastrowid
    body = (f"Proposal #{pid} on v{h['version']}: {describe_changes(changes)}. {rationale.strip()}"
            + (f" Expected: {expected if isinstance(expected, str) else json.dumps(expected)}" if expected else "")
            + (f" (supersedes #{', #'.join(map(str, old))})" if old else ""))
    for fid in ids:
        review.add_message(con, fid, author_role, author, body, kind="proposal", proposal_id=pid,
                           modeler_kind=kind if author_role == "modeler" else None)
    return get(con, pid)


def decide(con, proposal_id: int, action: str, reviewer: str, comment: str | None = None) -> dict:
    """The engineer's decision. accept: the modeler builds it; reject: the comment goes into the threads."""
    if action not in ("accept", "reject"):
        raise ToolError(INVALID_INPUT, "action must be accept or reject")
    p = get(con, proposal_id)
    if p["status"] != "proposed":
        raise ToolError(INVALID_INPUT, f"proposal {proposal_id} is {p['status']}")
    if action == "reject" and not (comment or "").strip():
        raise ToolError(INVALID_INPUT, "say why it is rejected (comment)")
    status = "accepted" if action == "accept" else "rejected"
    con.execute("UPDATE model_proposals SET status = ?, reviewer = ?, comment = ?, decided_at = datetime('now') "
                "WHERE id = ?", (status, reviewer, (comment or "").strip() or None, proposal_id))
    body = (f"Proposal #{proposal_id} accepted: build it." if status == "accepted"
            else f"Proposal #{proposal_id} rejected.") + (f" {comment.strip()}" if (comment or "").strip() else "")
    for fid in p["feedback_ids"]:
        review.add_message(con, fid, "reviewer", reviewer, body, kind=status, proposal_id=proposal_id)
    return get(con, proposal_id)


def mark_built(con, proposal_id: int, version_id: int) -> dict:
    p = get(con, proposal_id)
    if p["status"] != "accepted":
        raise ToolError(FORBIDDEN, f"proposal {proposal_id} is {p['status']}; only an accepted proposal is built")
    h = models.header(con, p["model_kind"], version_id)
    if h["model_name"] != p["model_name"]:
        raise ToolError(INVALID_INPUT, f"version {version_id} is of {h['model_name']}, not {p['model_name']}")
    con.execute("UPDATE model_proposals SET status = 'built', built_version_id = ? WHERE id = ?", (version_id, proposal_id))
    for fid in p["feedback_ids"]:
        if review.get(con, fid)["status"] == "open":
            review.add_message(con, fid, "modeler", f"{p['model_kind']} modeler",
                               f"Built v{h['version']} from proposal #{proposal_id} ({h['status']}).", kind="built",
                               proposal_id=proposal_id, version_id=version_id)
    return get(con, proposal_id)


def request(con, kind: str, base_version_id: int, feedback_ids: list[int], spec_patch: dict, rationale: str,
            reviewer: str, preview: dict | None = None) -> dict:
    """The engineer's own change (decision 4): a proposal by the reviewer, accepted at once, for the modeler to build."""
    p = propose(con, kind, base_version_id, feedback_ids, spec_patch, rationale, reviewer, author_role="reviewer",
                preview=preview)
    return decide(con, p["id"], "accept", reviewer)


def accepted_for_build(con, proposal_id: int, kind: str = "geo") -> dict:  # noqa: D401
    """The proposal a build is about to apply: it must be accepted and of this kind."""
    p = get(con, proposal_id)
    if p["model_kind"] != kind:
        raise ToolError(INVALID_INPUT, f"proposal {proposal_id} is for a {p['model_kind']} model")
    if p["status"] != "accepted":
        raise ToolError(FORBIDDEN, f"proposal {proposal_id} is {p['status']}: only an accepted proposal is built "
                                   "(the engineer accepts it in the workbench or with `opmagent review proposal`)")
    return p
