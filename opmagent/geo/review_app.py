"""Geo review workbench, phase 1: a local web app for the engineer (docs/design/geo-review.md, section 8).

`opmagent geo review <version>` serves the model panel (the QC library's figures, drawn from view specs) and the
review comments on the model. A click on a figure becomes an anchor (what the comment is about). A comment is
written to review_feedback as the reviewer, with its anchor and the view it was made on, so the geo agent gets
exactly what was meant (`get_review_feedback`, `review_context`).

The app is the engineer's, never an agent's:
- it binds 127.0.0.1 only and answers only requests whose Host is that address (no DNS rebinding);
- every write needs the token made at start, which is printed in the engineer's terminal and passed in the URL
  fragment (never sent in a URL query, written to disk or stored in the database);
- `opmagent setup` puts `opmagent geo review` under "ask" for the agents.
Approve and reject (design decision 1): in the app too, with three safeguards on top of the token:
- only a workbench started from a terminal (stdin a tty) can decide: one an agent started (no terminal) shows the
  `opmagent review` commands instead;
- the engineer types the model name and version to confirm, as `opmagent review approve` asks for "approve";
- approve is refused while a "change" comment on that version is open (questions and notes only warn).
The decision goes through the reviewer profile's approve / reject (audited), against the QC report the app serves.

Phase 2: threads (the engineer's replies; writing in an answered one reopens it), proposal accept / reject (the
reviewer profile's decide_geo_proposal, audited), the version diff, and the live session state (the geo agent
waiting in await_review_activity; the engineer ends it here).

The standard library's HTTP server, one request at a time: one engineer, one SQLite connection.
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core import db, models
from ..core import review as rv
from ..core.errors import FORBIDDEN, INVALID_INPUT, NOT_FOUND, ToolError
from ..servers.data_server import DataService, wrap
from ..core import proposals as props
from . import preview, review_diff

STATIC = Path(__file__).with_name("review_static")
MAX_BODY = 1 << 20


class ReviewApp:
    """The workbench's requests, without the socket (so tests call handle() directly)."""

    def __init__(self, cfg, version_id: int, token: str | None = None, kind: str = "geo", can_decide: bool = False):
        from ..reviews import check_workbench_kind
        from ..reviews import views as views_for
        self.kind = check_workbench_kind(kind)
        self.V = views_for(self.kind)
        self.cfg = cfg
        self.con = db.connect(cfg.database)
        self.reviewer = DataService("reviewer", cfg)
        self.token = token or secrets.token_urlsafe(24)
        self.version_id = int(version_id)
        self.can_decide = bool(can_decide)
        self.allowed_hosts: set[str] = set()
        self._cache: dict[int, dict] = {}
        models.header(self.con, self.kind, self.version_id)        # NOT_FOUND early
        self.data(self.version_id)                             # no payload: fail at start, not in the browser

    # ------------------------------------------------------------ data
    def data(self, vid: int) -> dict:
        if vid not in self._cache:
            self._cache[vid] = self.V.load_version(self.cfg, self.con, vid)
        d = self._cache[vid]
        d["header"]["status"] = models.header(self.con, self.kind, vid)["status"]   # the only thing that changes
        return d

    def _pins(self, vid: int) -> list[dict]:
        return [f for f in rv.list_feedback(self.con, kind=self.kind, version_id=vid, status=None) if f["anchor"]]

    # ------------------------------------------------------------ endpoints
    def meta(self, vid: int) -> dict:
        d = self.data(vid)
        h = models.header(self.con, self.kind, vid)
        versions = self.versions(h["model_name"])
        qc = self.reviewer.get_qc_report(kind=self.kind, version_id=vid)
        rep = (qc.get("qc_report") or {}) if qc.get("ok") else {}
        changeable = self.kind in props.KINDS
        from ..reviews import KINDS as THREADS
        out = {**self.V.meta(d), "kind": self.kind, "spec": d.get("spec") or {}, "can_propose": changeable,
               "threads": self.kind in THREADS,
               "can_preview": changeable and bool(props.base_spec(self.con, self.kind, vid)) and not d.get("db_only"),
               "versions": versions,
               "qc_report": rep.get("path")}
        out["decision"] = self.decision_state(vid)
        if h["status"] == "qc_ready":
            out["review_commands"] = [f"opmagent review approve {self.kind} {vid}",
                                      f"opmagent review reject {self.kind} {vid} --comment \"...\""]
        return out

    # ------------------------------------------------------------ approve / reject (design decision 1)
    def decision_state(self, vid: int) -> dict:
        h = models.header(self.con, self.kind, vid)
        open_ = rv.list_feedback(self.con, kind=self.kind, version_id=vid, status="open")
        return {"can_decide": self.can_decide, "status": h["status"], "confirm_text": f"{h['model_name']} v{h['version']}",
                "blockers": [f["id"] for f in open_ if f["severity"] == "change"],
                "open_other": [f["id"] for f in open_ if f["severity"] != "change"]}

    def decide_version(self, body: dict) -> dict:
        vid = int(body.get("version_id") or self.version_id)
        st = self.decision_state(vid)
        action = body.get("action")
        if action not in ("approve", "reject"):
            raise ToolError(INVALID_INPUT, "action must be approve or reject")
        if not self.can_decide:
            raise ToolError(FORBIDDEN, "this workbench was started without a terminal (by an agent?): approve or reject "
                                       f"in your terminal, `opmagent review {action} {self.kind} {vid}`")
        if st["status"] != "qc_ready":
            raise ToolError(INVALID_INPUT, f"v{models.header(self.con, self.kind, vid)['version']} is {st['status']}, "
                                           "not waiting for review")
        if (body.get("confirm") or "").strip() != st["confirm_text"]:
            raise ToolError(INVALID_INPUT, f"type {st['confirm_text']!r} to confirm")
        if action == "approve" and st["blockers"]:
            raise ToolError(INVALID_INPUT, "open change comment(s) on this version: "
                                           + ", ".join(f"#{i}" for i in st["blockers"])
                            + "; they are answered first (or reject the version)")
        qc = self._write("get_qc_report", kind=self.kind, version_id=vid)
        rid = (qc.get("qc_report") or {}).get("id")
        if not rid:
            raise ToolError(NOT_FOUND, "this version has no QC report to decide on")
        r = self._write(action, kind=self.kind, version_id=vid, qc_report_file_id=rid,
                        comment=(body.get("comment") or "").strip() or None)
        return {"header": {k: r["header"][k] for k in ("status", "version")}, "spoken_summary": r["spoken_summary"]}

    def preview_data(self, pid: int) -> dict:
        key = ("preview", pid)
        if key not in self._cache:
            from ..reviews import load_preview, preview_row
            if preview_row(self.con, pid)["model_kind"] != self.kind:
                raise ToolError(NOT_FOUND, f"no {self.kind} preview {pid}")
            self._cache[key] = load_preview(self.cfg, self.con, pid)
        return self._cache[key]

    def _spec(self, raw: dict):
        raw = raw or {}
        if raw.get("preview_id"):
            d = self.preview_data(int(raw["preview_id"]))
        else:
            d = self.data(int(raw.get("version_id") or self.version_id))
        b = self.data(int(raw["base_version_id"])) if raw.get("base_version_id") else None
        return self.V.check_spec(raw, d, b), d, b

    def view(self, body: dict) -> dict:
        spec, d, b = self._spec(body.get("spec"))
        fig = self.V.figure(spec, d, [] if d.get("preview_id") else self._pins(d["version_id"]), base=b)
        return {"spec": spec, "figure": json.loads(fig.to_json())}

    def locate(self, body: dict) -> dict:
        spec, d, _ = self._spec(body.get("spec"))
        a = self.V.locate(spec, d, body.get("role"), body.get("point") or {})
        if "comment" in a:
            return {"comment": a["comment"]}
        return {"anchor": a, "label": rv.label(a), "describe": self.V.describe(a, d)}

    def comments(self, vid: int) -> dict:
        name = models.header(self.con, self.kind, vid)["model_name"]
        cs = rv.list_feedback(self.con, kind=self.kind, model_name=name, status=None)
        for c in cs:
            c["messages"] = rv.messages(self.con, c["id"])
        ps = {p["id"]: p for p in props.list_proposals(self.con, self.kind, name)} if self.kind in props.KINDS else {}
        return {"comments": cs, "proposals": ps, "session": self.session(vid), "decision": self.decision_state(vid),
                "versions": self.versions(name, list(ps.values()), cs)}

    def versions(self, name: str, proposals: list[dict] | None = None, comments: list[dict] | None = None) -> list[dict]:
        """The model's version chain, oldest first: each version, its status and where it came from (the proposal it
        was built from, on which version; the comments it answered)."""
        if proposals is None:
            proposals = props.list_proposals(self.con, self.kind, name) if self.kind in props.KINDS else []
        if comments is None:
            comments = rv.list_feedback(self.con, kind=self.kind, model_name=name, status=None)
        built = {p["built_version_id"]: p for p in proposals if p.get("built_version_id")}
        addressed: dict[int, list[int]] = {}
        for c in comments:
            if c.get("addressed_in_version_id"):
                addressed.setdefault(c["addressed_in_version_id"], []).append(c["id"])
        out = []
        for m in sorted((m for m in models.list_models(self.con, self.kind) if m["model_name"] == name),
                        key=lambda m: m["version"]):
            p = built.get(m["version_id"])
            out.append({"version_id": m["version_id"], "version": m["version"], "status": m["status"],
                        "from_proposal": p["id"] if p else None, "base_version": p["base_version"] if p else None,
                        "addresses": sorted(addressed.get(m["version_id"], []))})
        return out

    def session(self, vid: int) -> dict:
        name = models.header(self.con, self.kind, vid)["model_name"]
        st = rv.session_state(self.con, self.kind, name)
        w = rv.waiting(self.con, self.kind, name)
        return {"listening": st["listening"], "open": bool(st["session"] and not st["session"]["ended_at"]),
                "waiting_on_agent": len(w["modeler"]), "waiting_on_you": len(w["reviewer"])}

    def _write(self, tool: str, **kw) -> dict:
        r = wrap(self.reviewer, tool)(**kw)
        if not r.get("ok"):
            raise ToolError(r.get("code", INVALID_INPUT), r.get("message", f"{tool} failed"))
        return r

    def message(self, body: dict) -> dict:
        fid = int(body.get("feedback_id") or 0)
        f = rv.get(self.con, fid)
        if f["model_kind"] != self.kind:
            raise ToolError(FORBIDDEN, f"comment {fid} is not on a {self.kind} model")
        return self._write("reply_review", feedback_id=fid, body=body.get("body", ""), views=body.get("views") or None)

    def decide(self, body: dict) -> dict:
        return self._write("decide_proposal", proposal_id=int(body.get("proposal_id") or 0),
                           action=body.get("action"), comment=body.get("comment"))

    def end_session(self, body: dict) -> dict:
        name = models.header(self.con, self.kind, int(body.get("version_id") or self.version_id))["model_name"]
        return {"ended": rv.session_end(self.con, self.kind, name, self.reviewer._reviewer())}

    def make_preview(self, body: dict) -> dict:
        r = self._write("preview_change", kind=self.kind, base_version_id=int(body.get("base_version_id") or self.version_id),
                        spec_patch=body.get("spec_patch"))
        return {"preview": r["preview"]}

    def request_change(self, body: dict) -> dict:
        """The engineer's previewed change as an accepted proposal. Without a thread to answer, the rationale
        becomes a general comment first, so the change has a thread like any other."""
        pid = int(body.get("preview_id") or 0)
        ids = [int(i) for i in body.get("feedback_ids") or []]
        rationale = (body.get("rationale") or "").strip()
        if not rationale:
            raise ToolError(INVALID_INPUT, "say why (rationale)")
        if not ids:
            p = preview.get(self.con, pid)
            if p["model_kind"] != self.kind:
                raise ToolError(NOT_FOUND, f"no {self.kind} preview {pid}")
            c = self._write("add_review_comment", kind=self.kind, version_id=p["base_version_id"], comment=rationale,
                            item=rv.make_item({}, None), severity="change")
            ids = [c["feedback"]["id"]]
        r = self._write("request_change", preview_id=pid, feedback_ids=ids, rationale=rationale)
        return {"proposal": r["proposal"]}

    def diff(self, vid: int, base: int | None) -> dict:
        return {"diff": review_diff.diff(self.cfg, self.con, vid, base, self.kind)}

    def add_comment(self, body: dict) -> dict:
        vid = int(body.get("version_id") or self.version_id)
        d = self.data(vid)
        anchor = self.V.check_anchor(body.get("anchor"))
        view = self._spec({**body["view"], "version_id": vid})[0] if body.get("view") else None
        if "finding" in anchor and not any(f"{f['area']}: {f['check']}" == anchor["finding"] for f in d["findings"]):
            raise ToolError(INVALID_INPUT, f"no QC finding {anchor['finding']!r} on this version")
        r = wrap(self.reviewer, "add_review_comment")(kind=self.kind, version_id=vid, comment=body.get("comment", ""),
                                                      item=rv.make_item(anchor, view),
                                                      severity=body.get("severity", "change"))
        if not r.get("ok"):
            raise ToolError(r.get("code", INVALID_INPUT), r.get("message", "comment not added"))
        return {k: v for k, v in r.items() if k != "ok"}

    # ------------------------------------------------------------ dispatch
    def handle(self, method: str, path: str, headers: dict, body: bytes = b"") -> tuple[int, str, bytes]:
        host = headers.get("host") or headers.get("Host") or ""
        if self.allowed_hosts and host not in self.allowed_hosts:
            return _json(403, {"ok": False, "code": FORBIDDEN, "message": "unexpected Host"})
        u = urlparse(path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if method == "GET" and u.path in ("/", "/index.html"):
                return 200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes()
            if method == "GET" and u.path == "/static/plotly.min.js":
                from plotly.offline import get_plotlyjs
                return 200, "application/javascript", get_plotlyjs().encode()
            if method == "GET" and u.path == "/qc":
                vid = int(q.get("version_id", self.version_id))
                qc = self.reviewer.get_qc_report(kind=self.kind, version_id=vid)
                p = Path(((qc.get("qc_report") or {}) if qc.get("ok") else {}).get("path") or "")
                if not p.is_file():
                    raise ToolError(NOT_FOUND, f"{self.kind} version {vid} has no QC report file")
                return 200, "text/html; charset=utf-8", p.read_bytes()
            if method == "GET" and u.path == "/api/meta":
                return _json(200, {"ok": True, **self.meta(int(q.get("version_id", self.version_id)))})
            if method == "GET" and u.path == "/api/comments":
                return _json(200, {"ok": True, **self.comments(int(q.get("version_id", self.version_id)))})
            if method == "GET" and u.path == "/api/preview":
                p = preview.get(self.con, int(q.get("id", 0)))
                if p["model_name"] != models.header(self.con, self.kind, self.version_id)["model_name"]:
                    raise ToolError(NOT_FOUND, "no such preview on this model")
                return _json(200, {"ok": True, "preview": p["summary"], "removed": bool(p["removed_at"])})
            if method == "GET" and u.path == "/api/session":
                return _json(200, {"ok": True, **self.session(int(q.get("version_id", self.version_id)))})
            if method == "GET" and u.path == "/api/diff":
                return _json(200, {"ok": True, **self.diff(int(q.get("version_id", self.version_id)),
                                                            int(q["base"]) if q.get("base") else None)})
            if method == "POST":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    raise ToolError(INVALID_INPUT, "body must be JSON") from None
                if u.path == "/api/view":
                    return _json(200, {"ok": True, **self.view(data)})
                if u.path == "/api/locate":
                    return _json(200, {"ok": True, **self.locate(data)})
                writes = {"/api/comments": self.add_comment, "/api/messages": self.message,
                          "/api/previews": self.make_preview, "/api/requests": self.request_change,
                          "/api/decide": self.decide_version,
                          "/api/proposals/decide": self.decide, "/api/session/end": self.end_session}
                if u.path in writes:
                    tok = headers.get("x-review-token") or headers.get("X-Review-Token") or ""
                    if not secrets.compare_digest(tok, self.token):
                        return _json(403, {"ok": False, "code": FORBIDDEN, "message": "the review token is missing or wrong; "
                                                                                       "open the URL printed by opmagent geo review"})
                    return _json(200, {"ok": True, **writes[u.path](data)})
            return _json(404, {"ok": False, "code": NOT_FOUND, "message": f"no {method} {u.path}"})
        except ToolError as e:
            return _json(404 if e.code == NOT_FOUND else 400, e.to_dict())
        except (ValueError, KeyError, TypeError) as e:
            return _json(400, {"ok": False, "code": INVALID_INPUT, "message": f"{type(e).__name__}: {e}"})


def _json(status: int, obj) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(obj, default=str).encode()


def make_server(app: ReviewApp, port: int = 0) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _go(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.send_error(413)
                return
            status, ctype, out = app.handle(method, self.path, {k.lower(): v for k, v in self.headers.items()},
                                            self.rfile.read(n) if n else b"")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802
            self._go("GET")

        def do_POST(self):  # noqa: N802
            self._go("POST")

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    p = srv.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    return srv


def url(srv: HTTPServer, app: ReviewApp) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/?version_id={app.version_id}#token={app.token}"
