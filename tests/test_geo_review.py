"""Geo review workbench, phase 1 (docs/design/geo-review.md): view specs, anchors from clicks, anchored comments
written as the reviewer behind the session token, and what the geo agent reads back."""
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from opmagent.core import review
from opmagent.geo import review_views as views
from opmagent.geo.review_app import ReviewApp, make_server, url
from opmagent.notebooks import geo_qc_lib as gq
from opmagent.servers.data_server import TOOLS, DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_qc_notebook import build  # noqa: E402


@pytest.fixture
def geo(cfg, tmp_path):
    g = build(cfg, tmp_path)
    assert g["ok"] and g["status"] == "qc_ready", g
    return g["version_id"]


def call(app, method, path, body=None, token=None):
    h = {"host": ""}
    if token:
        h["x-review-token"] = token
    st, ctype, out = app.handle(method, path, h, json.dumps(body).encode() if body is not None else b"")
    return st, json.loads(out) if ctype == "application/json" else out


def test_items_old_and_anchored():
    assert review.parse_item(None) == ({}, None)
    assert review.parse_item("PERMX layer 3") == ({"text": "PERMX layer 3"}, None)
    item = review.make_item({"cell": [9, 3, 2], "kw": "PORO", "well": "B"}, {"fig": "layer_map", "layer": 2})
    assert review.parse_item(item) == ({"cell": [9, 3, 2], "kw": "PORO", "well": "B"}, {"fig": "layer_map", "layer": 2})
    assert review.label({"cell": [9, 3, 2], "kw": "PORO", "well": "B"}) == "cell 9,3,2 B PORO"
    assert review.label({"surface": "MidRes", "well": "D"}) == "MidRes at D"
    assert review.label({}) == "general"
    with pytest.raises(Exception, match="unknown key"):
        views.check_anchor({"cell": [1, 1, 1], "code": "x"})
    with pytest.raises(Exception, match="1-based"):
        views.check_anchor({"cell": [0, 1, 1]})


def test_views_and_clicks(cfg, geo):
    app = ReviewApp(cfg, geo, token="T")
    st, m = call(app, "GET", "/api/meta")
    assert st == 200 and m["wells"] == ["A", "B", "C", "D"] and m["grid"] == [10, 10, 5]
    assert m["zones"] == [{"zone": "TopRes", "first": 1, "last": 3}, {"zone": "MidRes", "first": 4, "last": 5}]
    assert m["findings"] and m["review_commands"][0] == f"opmagent review approve geo {geo}"
    for spec in ({"fig": "surfaces"}, {"fig": "layer_map", "kw": "PERMX", "layer": 4},
                 {"fig": "section", "kw": "PORO", "along": "y", "cell": [3, 4]}, {"fig": "well", "well": "B"},
                 {"fig": "at_wells"}, {"fig": "distributions"}, {"fig": "poroperm"}):
        st, r = call(app, "POST", "/api/view", {"spec": spec})
        assert st == 200 and r["figure"]["data"], (spec, r)
    st, r = call(app, "POST", "/api/view", {"spec": {"fig": "layer_map", "kw": "PERMX", "layer": 4}})
    assert r["spec"]["log"] is True and [t["meta"]["role"] for t in r["figure"]["data"]] == ["layer", "wells"]
    # specs are data: only registered figures, known arguments, values inside the model
    for bad, msg in (({"fig": "exec"}, "fig"), ({"fig": "layer_map", "kw": "__class__"}, "keyword"),
                     ({"fig": "layer_map", "layer": 6}, "outside"), ({"fig": "well", "well": "Z"}, "no well"),
                     ({"fig": "surfaces", "path": "/etc"}, "unknown argument")):
        st, r = call(app, "POST", "/api/view", {"spec": bad})
        assert st == 400 and msg in r["message"], (bad, r)

    data = app.data(geo)
    wc = gq.well_cells(data).set_index("well_name")
    bi, bj = int(wc.loc["B", "i"]), int(wc.loc["B", "j"])
    # a cell on a layer map, in B's column: the cell (1-based), the keyword and the well
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "layer_map", "kw": "PORO", "layer": 2}, "role": "layer",
                                              "point": {"x": 1700, "y": 400}})
    assert r["anchor"] == {"cell": [bi + 1, bj + 1, 2], "kw": "PORO", "well": "B"}, r
    c = r["describe"]["cell"]
    assert c["values"]["PORO"] == pytest.approx(data["arrays"]["PORO"][1, bj, bi], rel=1e-3)
    assert c["zone"] == "TopRes" and c["nearest_well"]["well"] == "B"
    assert [lay["layer"] for lay in r["describe"]["well"]["at_layers"]] == [1, 2, 3, 4, 5]
    # a point on a section through A along x: the cell at that depth in A's row
    ai, aj = int(wc.loc["A", "i"]), int(wc.loc["A", "j"])
    col = data["layer_depths"][:, aj, 4]
    z = 0.5 * (col[1] + col[2])
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "section", "kw": "PORO", "along": "x", "well": "A"},
                                              "role": "section", "point": {"x": 900, "y": z}})
    assert r["anchor"] == {"cell": [5, aj + 1, 2], "kw": "PORO"}, r
    # a depth on a well track, a top on the surfaces, a point on the at-wells plot
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "well", "well": "C"}, "role": "well_track",
                                              "point": {"y": 2040, "kw": "PERMX"}})
    assert r["anchor"]["well"] == "C" and r["anchor"]["kw"] == "PERMX" and 1 <= r["anchor"]["layer"] <= 5
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "surfaces"}, "role": "tops",
                                              "point": {"text": "D", "surface": "MidRes"}})
    assert r["anchor"] == {"well": "D", "surface": "MidRes"} and "D" in r["describe"]["surface"]["misfit_m"]
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "at_wells"}, "role": "at_wells", "point": {"text": "B L3"}})
    assert r["anchor"] == {"well": "B", "layer": 3}


def test_anchored_comment_reaches_the_agent(cfg, geo):
    app = ReviewApp(cfg, geo, token="T")
    anchor = {"cell": [9, 3, 2], "kw": "PORO", "well": "B"}
    body = {"comment": "PORO at B in layer 2 looks high for this zone", "severity": "question", "anchor": anchor,
            "view": {"fig": "layer_map", "kw": "PORO", "layer": 2}}
    # writes need the session token
    assert call(app, "POST", "/api/comments", body)[0] == 403
    assert call(app, "POST", "/api/comments", body, token="wrong")[0] == 403
    st, r = call(app, "POST", "/api/comments", body, token="T")
    assert st == 200 and r["ok"], r
    fid = r["feedback"]["id"]
    assert r["feedback"]["reviewer"] and r["feedback"]["anchor"] == anchor
    # a finding must be one of this version's
    st, r = call(app, "POST", "/api/comments", {"comment": "x", "anchor": {"finding": "nope: nothing"}}, token="T")
    assert st == 400 and "no QC finding" in r["message"]
    # the pin is drawn on that layer's map, not on another layer's
    roles = lambda lay: [t["meta"]["role"] for t in call(app, "POST", "/api/view", {"spec": {"fig": "layer_map", "layer": lay}})[1]["figure"]["data"]]  # noqa: E731
    assert "pins" in roles(2) and "pins" not in roles(3)
    st, r = call(app, "POST", "/api/locate", {"spec": {"fig": "layer_map", "layer": 2}, "role": "pins", "point": {"text": f"#{fid}"}})
    assert r == {"ok": True, "comment": fid}
    st, r = call(app, "GET", "/api/comments")
    assert [c["label"] for c in r["comments"]] == ["cell 9,3,2 B PORO"]

    # the geo agent: the feedback carries the anchor and view; review_context says what is there
    g = DataService("geo", cfg)
    f = wrap(g, "get_review_feedback")()
    assert f["feedback"][0]["anchor"] == anchor and f["feedback"][0]["view"]["layer"] == 2
    assert "[cell 9,3,2 B PORO]" in f["spoken_summary"]
    ctx = wrap(g, "review_context")(feedback_id=fid)
    assert ctx["ok"], ctx
    data = app.data(geo)
    assert ctx["at_anchor"]["cell"]["values"]["PORO"] == pytest.approx(data["arrays"]["PORO"][1, 2, 8], rel=1e-3)
    assert ctx["view"]["shows"].startswith("PORO map of layer 2") and set(ctx["view"]["at_wells"]) == {"A", "B", "C", "D"}
    up = next(lay for lay in ctx["at_anchor"]["well"]["at_layers"] if lay["layer"] == 2)
    assert abs(up["phi_model"] - up["phi_log"]) < 0.01
    v = wrap(g, "render_review_view")(spec={"fig": "section", "version_id": geo, "kw": "PORO", "well": "B"},
                                      anchor={"layer": 2})
    assert v["ok"] and len(v["by_layer_mean"]) == 5 and v["at_anchor"]["layer"]["zone"] == "TopRes", v
    assert wrap(g, "render_review_view")(spec={"fig": "well", "version_id": geo, "well": "Q"})["code"] == "INVALID_INPUT"
    # an older free-text comment still reads, without a context to compute
    rv = DataService("reviewer", cfg)
    old = rv.add_review_comment(kind="geo", version_id=geo, comment="check NTG", item="NTG layer 1")["feedback"]
    assert old["anchor"] == {"text": "NTG layer 1"} and old["label"] == "NTG layer 1"
    assert wrap(g, "review_context")(feedback_id=old["id"])["at_anchor"] is None


def test_tools_and_profiles():
    assert {"review_context", "render_review_view"} <= set(TOOLS["geo"]) & set(TOOLS["reviewer"])
    assert "add_review_comment" not in TOOLS["geo"]
    # phase 4: res and well review too; only geo and res propose; nobody but the reviewer decides
    assert {"review_context", "reply_review", "await_review_activity"} <= set(TOOLS["res"]) & set(TOOLS["well"])
    assert "propose_res_change" in TOOLS["res"] and not [t for t in TOOLS["well"] if "propose" in t or "preview" in t]
    assert not [t for p in ("geo", "res", "well") for t in TOOLS[p] if t.startswith(("decide", "request"))]


def test_served_on_localhost_only(cfg, geo):
    app = ReviewApp(cfg, geo)
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        link = url(srv, app)
        assert link.startswith("http://127.0.0.1:") and f"#token={app.token}" in link
        assert srv.server_address[0] == "127.0.0.1"
        base = link.split("/?")[0]
        assert b"Geo review" in urllib.request.urlopen(base + "/").read()
        meta = json.loads(urllib.request.urlopen(f"{base}/api/meta?version_id={geo}").read())
        assert meta["version_id"] == geo
        req = urllib.request.Request(base + "/api/comments", method="POST", data=json.dumps(
            {"comment": "general remark", "severity": "note"}).encode(), headers={"X-Review-Token": app.token})
        assert json.loads(urllib.request.urlopen(req).read())["feedback"]["label"] == "general"
        # another Host (DNS rebinding) is refused
        req = urllib.request.Request(base + "/api/meta", headers={"Host": "attacker.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        assert b"FieldG_Geo version 1" in urllib.request.urlopen(f"{base}/qc?version_id={geo}").read()   # the QC report
    finally:
        srv.shutdown()
        srv.server_close()


def test_section_through_a_cell(cfg, geo):
    data = ReviewApp(cfg, geo).data(geo)
    fig = gq.fig_section(data, "PORO", along="y", cell=(2, 5))
    assert "cell 3, 6" in fig.layout.title.text and "column i 3" in fig.layout.title.text
    assert np.isfinite(np.asarray(fig.data[0].z, dtype=float)).any()
