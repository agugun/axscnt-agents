"""The review workbench for res and well models (docs/design/geo-review.md, phase 4): res as geo (views, anchored
comments, threads, proposals and what-if previews on its spec); well reviewed only (views, comments, threads, the live
session; its changes are data edits, so no proposals)."""
import json

import pytest

from opmagent.core import db, proposals, review
from opmagent.geo.review_app import ReviewApp
from opmagent.reviews import build_from_proposal
from opmagent.servers.data_server import TOOLS, DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_modeler import SPEC  # noqa: E402
from tests.test_geo_qc_notebook import build  # noqa: E402

RES_SPEC = {"scal": [{"satnum": 1, "swc": 0.2, "sorw": 0.2}, {"satnum": 2, "swc": 0.25, "sorw": 0.25, "nw": 3}],
            "contacts": SPEC["contacts"], "regions": {"SATNUM": {"by": "zone", "values": {"TopRes": 1, "MidRes": 2}}}}


def post(app, path, body, token="T"):
    st, _, out = app.handle("POST", path, {"host": "", "x-review-token": token}, json.dumps(body).encode())
    return st, json.loads(out)


def get(app, path):
    st, _, out = app.handle("GET", path, {"host": ""})
    return st, json.loads(out)


def tool(svc, name, **kw):
    return wrap(svc, name)(**kw)


@pytest.fixture
def res_v1(cfg, tmp_path):
    from opmagent.res.pipeline import build_res_model
    g = build(cfg, tmp_path)
    r = build_res_model(cfg, spec=RES_SPEC, name="FieldG_Res", geo_version_id=g["version_id"])
    assert r["ok"] and r["status"] == "qc_ready", r
    return r["version_id"]


def test_res_loop(cfg, res_v1):
    app = ReviewApp(cfg, res_v1, token="T", kind="res")
    res, rv = DataService("res", cfg), DataService("reviewer", cfg)
    st, m = get(app, f"/api/meta?version_id={res_v1}")
    assert st == 200 and m["kind"] == "res" and m["can_preview"] and m["can_propose"]
    assert {"kr", "regions", "contacts", "contact_section"} <= set(m["views"]) and m["satnums"] == [1, 2]
    assert m["review_commands"][0] == f"opmagent review approve res {res_v1}"
    for spec in ({"fig": "kr"}, {"fig": "kr", "satnum": 2}, {"fig": "regions", "kw": "SATNUM", "layer": 4},
                 {"fig": "contacts"}, {"fig": "contact_section", "kw": "EQLNUM", "along": "y", "index": 2}):
        st, r = post(app, "/api/view", {"spec": spec})
        assert st == 200, (spec, r)
    for bad in ({"fig": "layer_map"}, {"fig": "kr", "satnum": 9}, {"fig": "regions", "kw": "BOGUS"}):
        assert post(app, "/api/view", {"spec": bad})[0] == 400, bad
    # clicks: a curve, a contact, a cell on the section (its layer from the point's customdata)
    st, r = post(app, "/api/locate", {"spec": {"fig": "kr"}, "role": "curve",
                                      "point": {"x": 0.5, "satnum": 2, "curve": "krw"}})
    assert r["anchor"] == {"satnum": 2, "curve": "krw", "s": 0.5} and r["describe"]["satnum"]["swc"] == 0.25
    st, r = post(app, "/api/locate", {"spec": {"fig": "regions", "kw": "SATNUM", "layer": 4}, "role": "layer",
                                      "point": {"x": 3, "y": 3}})
    assert r["anchor"] == {"cell": [3, 3, 4], "kw": "SATNUM"} and r["describe"]["cell"]["values"]["SATNUM"] == 2
    assert r["describe"]["cell"]["interval"] == "oil column"
    st, r = post(app, "/api/locate", {"spec": {"fig": "contact_section", "kw": "EQLNUM", "along": "x", "index": 3},
                                      "role": "section_cell", "point": {"x": 4, "y": 2030, "customdata": [2, 1]}})
    assert r["anchor"]["cell"] == [4, 3, 2]
    # the engineer pins a question on the OWC; the res agent hears it and sees the pore volumes
    st, r = post(app, "/api/comments", {"comment": "OWC 2100 looks deep: the RFT says about 2060", "severity": "question",
                                        "anchor": {"eqlnum": 1, "contact": "OWC"}, "view": {"fig": "contacts"}})
    assert st == 200 and r["feedback"]["label"] == "EQLNUM 1 OWC", r
    fid = r["feedback"]["id"]
    w = tool(res, "await_review_activity", version_id=res_v1, timeout_s=3)
    assert w["event"] == "waiting_on_you" and w["threads"][0]["id"] == fid
    ctx = tool(res, "review_context", feedback_id=fid)
    assert ctx["ok"] and ctx["at_anchor"]["eqlnum"]["owc_depth"] == 2100 and ctx["at_anchor"]["eqlnum"]["pv_oil"] > 0
    assert ctx["view"]["pore_volumes"]["1"]["pv_oil"] > 0 if "1" in ctx["view"]["pore_volumes"] else \
        ctx["view"]["pore_volumes"][1]["pv_oil"] > 0
    # it previews the change: oil pore volume moves to water, no Flow run
    p = tool(res, "preview_res_change", base_version_id=res_v1, spec_patch={"contacts": {"owc_depth": 2060}})
    assert p["ok"] and p["preview"]["elapsed_s"] < 30, p
    moved = {n["key"]: n for n in p["preview"]["numbers"] if n["change"]}
    assert moved["eqlnum.1.pv_oil"]["change"] < 0 and moved["eqlnum.1.pv_water"]["change"] == -moved["eqlnum.1.pv_oil"]["change"]
    assert [c["key"] for c in p["preview"]["changes"]] == ["contacts.owc_depth"]
    v = tool(res, "render_review_view", spec={"fig": "contacts", "preview_id": p["preview"]["preview_id"],
                                             "base_version_id": res_v1})
    assert v["ok"] and v["contacts"][0]["owc_depth"] == 2060 and v["base_contacts"][0]["owc_depth"] == 2100, v
    assert tool(res, "reply_review", feedback_id=fid, body="The RFT fits 2060: oil PV drops by the amount shown.",
                views=[{"fig": "contacts", "preview_id": p["preview"]["preview_id"], "base_version_id": res_v1}])["ok"]
    # ... proposes it with the preview; checks first
    for bad_patch in ({"surfaces": []}, {"regions": {"SATNUM": {"by": "zone", "values": {"Nope": 1}}}},
                      {"contacts": {"datum_pressure": None}}):
        assert tool(res, "propose_res_change", base_version_id=res_v1, feedback_ids=[fid], spec_patch=bad_patch,
                    rationale="x")["code"] == "INVALID_INPUT", bad_patch
    pr = tool(res, "propose_res_change", base_version_id=res_v1, feedback_ids=[fid],
              spec_patch={"contacts": {"owc_depth": 2060}}, rationale="the RFT", preview_id=p["preview"]["preview_id"])
    assert pr["ok"] and pr["proposal"]["model_kind"] == "res", pr
    pid = pr["proposal"]["id"]
    with pytest.raises(Exception, match="only an accepted proposal"):
        build_from_proposal(cfg, "res", pid)
    assert tool(rv, "decide_proposal", proposal_id=pid, action="accept")["ok"]
    b = build_from_proposal(cfg, "res", pid)
    assert b["ok"] and b["status"] == "qc_ready" and any("res proposal" in a for a in b["assumptions"]), b
    con = db.connect(cfg.database)
    assert proposals.get(con, pid)["built_version_id"] == b["version_id"]
    # the diff: the contact and the pore volumes, and the thread it answers
    d = tool(res, "review_diff", version_id=b["version_id"])["diff"]
    assert d["base_version_id"] == res_v1 and [s["key"] for s in d["spec"]] == ["contacts.owc_depth"]
    assert {n["key"] for n in d["numbers"] if n["change"]} == {"eqlnum.1.owc_depth", "eqlnum.1.pv_oil", "eqlnum.1.pv_water"}
    assert [t["id"] for t in d["threads"]] == [fid]
    assert tool(res, "answer_review_feedback", feedback_id=fid, response="v2: OWC 2060", status="addressed",
                addressed_in_version_id=b["version_id"])["ok"]

    # the engineer's own change on v2: SATNUM 2's nw, previewed and requested; the res agent builds it
    app2 = ReviewApp(cfg, b["version_id"], token="T", kind="res")
    patch = {"scal": [{"satnum": 1, "swc": 0.2, "sorw": 0.2}, {"satnum": 2, "swc": 0.25, "sorw": 0.25, "nw": 2.5}]}
    st, r = post(app2, "/api/previews", {"base_version_id": b["version_id"], "spec_patch": patch})
    assert st == 200 and [c["key"] for c in r["preview"]["changes"]] == ["scal.2.nw"], r
    st, kr = post(app2, "/api/view", {"spec": {"fig": "kr", "preview_id": r["preview"]["preview_id"],
                                               "base_version_id": b["version_id"]}})
    assert st == 200 and any("(v2)" in t["name"] for t in kr["figure"]["data"])        # the base's curves, dotted
    st, q = post(app2, "/api/requests", {"preview_id": r["preview"]["preview_id"], "rationale": "SCAL lab: nw 2.5"})
    assert st == 200 and q["proposal"]["status"] == "accepted" and q["proposal"]["author_role"] == "reviewer", q
    b3 = build_from_proposal(cfg, "res", q["proposal"]["id"])
    assert b3["ok"] and any("by the engineer" in a for a in b3["assumptions"]), b3
    # a res preview isn't a geo one, and the other way round
    assert tool(DataService("geo", cfg), "render_review_view", spec={"fig": "layer_map", "preview_id":
                r["preview"]["preview_id"]})["code"] == "INVALID_INPUT"


def test_well_review_only(cfg, tmp_path):
    from opmagent.hello import models as hm
    from opmagent.well.pipeline import build_well_model
    from tests.test_well_modeler import write_inputs
    g = hm.build_geo(cfg, origin="built", name="W_Geo")
    f = {k: str(v) for k, v in write_inputs(tmp_path).items()}
    r = build_well_model(cfg, f, "W_Wells", geo_version_id=g["version_id"])
    assert r["ok"], r
    vid = r["version_id"]
    app = ReviewApp(cfg, vid, token="T", kind="well")
    well = DataService("well", cfg)
    st, m = get(app, f"/api/meta?version_id={vid}")
    assert m["kind"] == "well" and not m["can_preview"] and not m["can_propose"] and m["wells"] == ["I1", "P1"]
    assert {"plan", "well_section", "history", "schedule"} <= set(m["views"])
    for spec in ({"fig": "plan"}, {"fig": "well_section", "well": "P1"}, {"fig": "perforated_cells"},
                 {"fig": "history", "well": "I1"}, {"fig": "cumulative"}, {"fig": "schedule"}):
        st, v = post(app, "/api/view", {"spec": spec})
        assert st == 200, (spec, v)
    assert post(app, "/api/view", {"spec": {"fig": "history", "well": "X9"}})[0] == 400
    st, v = post(app, "/api/view", {"spec": {"fig": "well_section", "well": "P1"}})
    perf = next(t["meta"] for t in v["figure"]["data"] if t["meta"]["role"] == "perforation")
    st, loc = post(app, "/api/locate", {"spec": {"fig": "well_section", "well": "P1"}, "role": "perforation", "point": perf})
    assert loc["anchor"] == {"well": "P1", "perforation": perf["perforation"]} and loc["describe"]["perforation_cells"]
    st, loc2 = post(app, "/api/locate", {"spec": {"fig": "history", "well": "P1"}, "role": "history",
                                         "point": {"x": "2026-03-01", "well": "P1", "quantity": "oil_rate"}})
    assert loc2["anchor"] == {"well": "P1", "date": "2026-03-01", "quantity": "oil_rate"}
    assert loc2["describe"]["event"]["oil_rate"] > 0
    st, c = post(app, "/api/comments", {"comment": "P1's March rate looks low", "anchor": loc2["anchor"],
                                        "view": {"fig": "history", "well": "P1"}})
    assert st == 200 and c["feedback"]["label"] == "P1 2026-03-01 oil_rate"
    fid = c["feedback"]["id"]
    w = tool(well, "await_review_activity", version_id=vid, timeout_s=3)
    assert w["event"] == "waiting_on_you" and w["threads"][0]["id"] == fid
    ctx = tool(well, "review_context", feedback_id=fid)
    assert ctx["ok"] and ctx["at_anchor"]["event"]["oil_rate"] > 0 and "P1" in ctx["view"]["shows"]
    assert tool(well, "reply_review", feedback_id=fid, body="March had downtime: the data manager is asked to check.",
                views=[{"fig": "schedule"}])["ok"]
    assert review.get(app.con, fid)["waiting_on"] == "reviewer"
    # no proposals or previews for well: its changes are data edits
    assert not [t for t in TOOLS["well"] if "propose" in t or "preview" in t]
    rv = DataService("reviewer", cfg)
    assert tool(rv, "preview_change", kind="well", base_version_id=vid, spec_patch={"x": 1})["code"] == "INVALID_INPUT"
    assert post(app, "/api/previews", {"base_version_id": vid, "spec_patch": {"x": 1}})[0] == 400
    # a second version: the diff in well terms
    r2 = build_well_model(cfg, f, "W_Wells", geo_version_id=g["version_id"])
    d = tool(well, "review_diff", version_id=r2["version_id"])["diff"]
    nums = {n["key"]: n for n in d["numbers"]}
    assert d["base_version_id"] == vid and nums["wells"]["new"] == 2 and nums["perforations"]["change"] == 0


def test_hello_res_and_well_in_the_workbench(cfg):
    """hello's models: res and well read whatever they hold (no payload: tables from the database)."""
    from opmagent.hello.run import hello, latest_demo
    hello(cfg, log=lambda *_: None)
    demo = latest_demo(db.connect(cfg.database))
    for kind in ("res", "well"):
        app = ReviewApp(cfg, demo[kind]["version_id"], token="T", kind=kind)
        st, m = get(app, f"/api/meta?version_id={demo[kind]['version_id']}")
        assert st == 200 and m["views"], (kind, m)
        st, v = post(app, "/api/view", {"spec": {"fig": m["views"][0]}})
        assert st == 200, (kind, v)


def test_proposal_kinds(cfg):
    con = db.connect(cfg.database)
    con.execute("INSERT INTO model_proposals(model_kind, model_name, base_version_id, feedback_ids_json, spec_patch_json, "
                "rationale, author) VALUES ('res', 'R', 1, '[]', '{}', 'x', 'a')")
    with pytest.raises(Exception, match="CHECK"):
        con.execute("INSERT INTO model_proposals(model_kind, model_name, base_version_id, feedback_ids_json, "
                    "spec_patch_json, rationale, author) VALUES ('well', 'W', 1, '[]', '{}', 'x', 'a')")
    assert proposals.spec_diff({"scal": [{"satnum": 2, "nw": 3}]}, {"scal": [{"satnum": 2, "nw": 2.5}]}) == \
        [{"key": "scal.2.nw", "old": 3, "new": 2.5}]
