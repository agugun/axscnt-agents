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
Approve and reject stay in `opmagent review` (the app shows the command).

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
from . import review_views as views

STATIC = Path(__file__).with_name("review_static")
MAX_BODY = 1 << 20


class ReviewApp:
    """The workbench's requests, without the socket (so tests call handle() directly)."""

    def __init__(self, cfg, version_id: int, token: str | None = None):
        self.cfg = cfg
        self.con = db.connect(cfg.database)
        self.reviewer = DataService("reviewer", cfg)
        self.token = token or secrets.token_urlsafe(24)
        self.version_id = int(version_id)
        self.allowed_hosts: set[str] = set()
        self._cache: dict[int, dict] = {}
        models.header(self.con, "geo", self.version_id)        # NOT_FOUND early
        self.data(self.version_id)                             # no payload: fail at start, not in the browser

    # ------------------------------------------------------------ data
    def data(self, vid: int) -> dict:
        if vid not in self._cache:
            self._cache[vid] = views.load_version(self.cfg, self.con, vid)
        d = self._cache[vid]
        d["header"]["status"] = models.header(self.con, "geo", vid)["status"]   # the only thing that changes
        return d

    def _pins(self, vid: int) -> list[dict]:
        return [f for f in rv.list_feedback(self.con, kind="geo", version_id=vid, status=None) if f["anchor"]]

    # ------------------------------------------------------------ endpoints
    def meta(self, vid: int) -> dict:
        d = self.data(vid)
        h = models.header(self.con, "geo", vid)
        versions = [{"version_id": m["version_id"], "version": m["version"], "status": m["status"]}
                    for m in models.list_models(self.con, "geo") if m["model_name"] == h["model_name"]]
        qc = self.reviewer.get_qc_report(kind="geo", version_id=vid)
        rep = (qc.get("qc_report") or {}) if qc.get("ok") else {}
        out = {**views.meta(d), "versions": sorted(versions, key=lambda v: v["version"]),
               "qc_report": rep.get("path"), "views": list(views.FIGS)}
        if h["status"] == "qc_ready":
            out["review_commands"] = [f"opmagent review approve geo {vid}",
                                      f"opmagent review reject geo {vid} --comment \"...\""]
        return out

    def view(self, body: dict) -> dict:
        vid = int(body.get("spec", {}).get("version_id") or self.version_id)
        d = self.data(vid)
        spec = views.check_spec(body.get("spec") or {}, d)
        fig = views.figure(spec, d, self._pins(vid))
        return {"spec": spec, "figure": json.loads(fig.to_json())}

    def locate(self, body: dict) -> dict:
        vid = int(body.get("spec", {}).get("version_id") or self.version_id)
        d = self.data(vid)
        spec = views.check_spec(body.get("spec") or {}, d)
        a = views.locate(spec, d, body.get("role"), body.get("point") or {})
        if "comment" in a:
            return {"comment": a["comment"]}
        return {"anchor": a, "label": views.label(a), "describe": views.describe(a, d)}

    def comments(self, vid: int) -> dict:
        name = models.header(self.con, "geo", vid)["model_name"]
        return {"comments": rv.list_feedback(self.con, kind="geo", model_name=name, status=None)}

    def add_comment(self, body: dict) -> dict:
        vid = int(body.get("version_id") or self.version_id)
        d = self.data(vid)
        anchor = views.check_anchor(body.get("anchor"))
        view = views.check_spec(body["view"], d) if body.get("view") else None
        if "finding" in anchor and not any(f"{f['area']}: {f['check']}" == anchor["finding"] for f in d["findings"]):
            raise ToolError(INVALID_INPUT, f"no QC finding {anchor['finding']!r} on this version")
        r = wrap(self.reviewer, "add_review_comment")(kind="geo", version_id=vid, comment=body.get("comment", ""),
                                                      item=rv.make_item(anchor, view),
                                                      severity=body.get("severity", "change"))
        if not r.get("ok"):
            raise ToolError(r.get("code", INVALID_INPUT), r.get("message", "comment not added"))
        return r

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
                qc = self.reviewer.get_qc_report(kind="geo", version_id=vid)
                p = Path(((qc.get("qc_report") or {}) if qc.get("ok") else {}).get("path") or "")
                if not p.is_file():
                    raise ToolError(NOT_FOUND, f"geo version {vid} has no QC report file")
                return 200, "text/html; charset=utf-8", p.read_bytes()
            if method == "GET" and u.path == "/api/meta":
                return _json(200, {"ok": True, **self.meta(int(q.get("version_id", self.version_id)))})
            if method == "GET" and u.path == "/api/comments":
                return _json(200, {"ok": True, **self.comments(int(q.get("version_id", self.version_id)))})
            if method == "POST":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    raise ToolError(INVALID_INPUT, "body must be JSON") from None
                if u.path == "/api/view":
                    return _json(200, {"ok": True, **self.view(data)})
                if u.path == "/api/locate":
                    return _json(200, {"ok": True, **self.locate(data)})
                if u.path == "/api/comments":
                    tok = headers.get("x-review-token") or headers.get("X-Review-Token") or ""
                    if not secrets.compare_digest(tok, self.token):
                        return _json(403, {"ok": False, "code": FORBIDDEN, "message": "the review token is missing or wrong; "
                                                                                       "open the URL printed by opmagent geo review"})
                    return _json(200, self.add_comment(data))
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
