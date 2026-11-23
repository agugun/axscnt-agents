"""The initialisation gate: a deck runs in full only after a person has accepted its initial state.

Every validated deck gets an initialisation view (init_view.py): the dynamic model at time zero in OPM Flow, with
the fluids in place and the contact checks. Before the full simulation, a person looks at it and accepts it, or
rejects it with a comment to the modeler concerned. The decision is recorded in `init_reviews` (append-only), with
a copy of the view it was made on (init_view.json and its sha256).

    accept(...)    human, at an interactive terminal (opmagent review accept-init) or the reviewer server
    reject(...)    the same, with a comment, also filed as review feedback on the deck's fluid, geo, res or well model
    auto_accept    with [study] auto_approve: accepted as AUTO_REVIEWER, but only when every check passes (or is n/a)
    require(...)   the full run (run_simulation, opmagent run) and the study's deck task refuse a deck not accepted

Import round trips (deck_import verify) are exempt: they compare a rebuilt deck with its source, not a forecast.
"""
from __future__ import annotations

import json

from ..core import review
from ..core.errors import INVALID_INPUT, ToolError
from ..core.files import sha256_of

AUTO_REVIEWER = "auto (approval disabled)"
NOT_ACCEPTED = "INIT_NOT_ACCEPTED"
KINDS = ("fluid", "geo", "res", "well")


def _view(cfg, con, deck_id: int) -> tuple[dict, str]:
    from .init_view import folder_of
    p = folder_of(cfg, con, deck_id) / "init_view.json"
    if not p.is_file():
        raise ToolError(INVALID_INPUT, f"deck {deck_id} has no initialisation view (it failed, or was never made): "
                                       f"make it with opmagent deck init {deck_id}, and fix the deck if Flow "
                                       "can't initialise it")
    return json.loads(p.read_text()), sha256_of(p)


def latest(con, deck_id: int) -> dict | None:
    r = con.execute("SELECT * FROM init_reviews WHERE deck_id = ? ORDER BY id DESC LIMIT 1", (deck_id,)).fetchone()
    return dict(r) if r else None


def state(con, deck_id: int) -> str:
    """accepted, rejected or waiting (no decision yet)."""
    r = latest(con, deck_id)
    return r["decision"] if r else "waiting"


def pending(con) -> list[dict]:
    """Validated decks whose initial state has no decision yet, newest first."""
    rows = con.execute("SELECT d.id, d.name, d.created_at FROM decks d WHERE d.status = 'validated' AND NOT EXISTS "
                       "(SELECT 1 FROM init_reviews r WHERE r.deck_id = d.id) ORDER BY d.id DESC").fetchall()
    return [dict(r) for r in rows]


def require(con, deck_id: int) -> None:
    s = state(con, deck_id)
    if s == "accepted":
        return
    if s == "rejected":
        r = latest(con, deck_id)
        raise ToolError(NOT_ACCEPTED, f"the initial state of deck {deck_id} was rejected by {r['reviewer']} "
                                      f"({r['comment']}): rebuild the deck after the fix")
    raise ToolError(NOT_ACCEPTED, f"deck {deck_id}'s initial state hasn't been accepted yet: look at it "
                                  f"(opmagent results open --deck {deck_id} --init) and accept it with "
                                  f"opmagent review accept-init {deck_id}, or reject it with a comment")


def _record(con, deck_id, decision, reviewer, comment, view, sha):
    if state(con, deck_id) != "waiting":
        r = latest(con, deck_id)
        raise ToolError(INVALID_INPUT, f"deck {deck_id}'s initial state was already {r['decision']} by "
                                       f"{r['reviewer']} at {r['decided_at']}; rebuild the deck to review it again")
    con.execute("INSERT INTO init_reviews(deck_id, decision, reviewer, comment, init_view_sha256, view_json) "
                "VALUES (?,?,?,?,?,?)", (deck_id, decision, reviewer, comment, sha, json.dumps(view)))
    return latest(con, deck_id)


def summary_lines(view: dict) -> list[str]:
    ip, u = view.get("in_place") or {}, view.get("units") or {}
    out = [f"oil in place {ip.get('FOIP', 0):,.0f} {u.get('FOIP', '')}, gas {ip.get('FGIP', 0):,.0f} "
           f"{u.get('FGIP', '')}, water {ip.get('FWIP', 0):,.0f} {u.get('FWIP', '')}, average pressure "
           f"{ip.get('FPR', 0):,.1f} {u.get('FPR', '')}"]
    if len(view.get("regions") or []) > 1:
        out.append(f"{len(view['regions'])} fluid-in-place regions")
    out += [f"[{c['status']}] {c['check']}" + (f": {c['detail']}" if c.get("detail") else "")
            for c in view.get("checks") or []]
    return out


def accept(cfg, con, deck_id: int, reviewer: str, comment: str | None = None) -> dict:
    view, sha = _view(cfg, con, deck_id)
    r = _record(con, deck_id, "accepted", reviewer, comment, view, sha)
    return {"ok": True, "review": r, "spoken_summary": f"Initial state of deck {deck_id} accepted: it can run."}


def reject(cfg, con, deck_id: int, reviewer: str, comment: str, to: str = "res") -> dict:
    if not (comment or "").strip():
        raise ToolError(INVALID_INPUT, "a rejection needs a comment saying what is wrong")
    if to not in KINDS:
        raise ToolError(INVALID_INPUT, f"--to must be one of {KINDS}: the model the comment is for")
    view, sha = _view(cfg, con, deck_id)
    d = con.execute(f"SELECT {to}_version_id AS vid FROM decks WHERE id = ?", (deck_id,)).fetchone()
    r = _record(con, deck_id, "rejected", reviewer, comment.strip(), view, sha)
    fb = review.add_comment(con, to, d["vid"], f"initial state of deck {deck_id}: {comment.strip()}", reviewer,
                            "initial state", "change")
    return {"ok": True, "review": r, "feedback": fb,
            "spoken_summary": f"Initial state of deck {deck_id} rejected; the comment went to {to} "
                              f"{fb['model_name']} v{fb['version']}."}


def auto_accept(cfg, con, deck_id: int, view: dict | None) -> dict | None:
    """With [study] auto_approve, accept when every check passes or is n/a; else leave it for a person."""
    if not getattr(cfg, "auto_approve", False) or not view:
        return None
    bad = [c for c in view.get("checks") or [] if c["status"] not in ("pass", "n/a")]
    if bad:
        return {"auto": False, "reason": "; ".join(c["check"] for c in bad)}
    v, sha = _view(cfg, con, deck_id)
    _record(con, deck_id, "accepted", AUTO_REVIEWER, "every initialisation check passes", v, sha)
    return {"auto": True}
