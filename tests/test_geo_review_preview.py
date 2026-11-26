"""Geo review workbench, phase 3 (docs/design/geo-review.md): what-if previews (the agent's and the engineer's),
the engineer's own proposals (decision 4), and models without a QC payload (from a deck, or hello's) in the workbench."""
import json
from dataclasses import replace

import numpy as np
import pytest

from opmagent.core import db, proposals, review
from opmagent.geo import preview as pv
from opmagent.geo.proposal_build import build_from_proposal
from opmagent.geo.review_app import ReviewApp
from opmagent.servers.data_server import TOOLS, DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_qc_notebook import build  # noqa: E402

PATCH = {"petro": {"vsh_cutoff": 0.4}, "layers": [4, 2]}


def post(app, path, body, token="T"):
    st, _, out = app.handle("POST", path, {"host": "", "x-review-token": token}, json.dumps(body).encode())
    return st, json.loads(out)


def get(app, path):
    st, _, out = app.handle("GET", path, {"host": ""})
    return st, json.loads(out)


def tool(svc, name, **kw):
    return wrap(svc, name)(**kw)


@pytest.fixture
def v1(cfg, tmp_path):
    g = build(cfg, tmp_path)
    assert g["ok"], g
    return g["version_id"]


def test_agent_preview_then_proposal(cfg, v1):
    geo = DataService("geo", cfg)
    p = tool(geo, "preview_geo_change", base_version_id=v1, spec_patch=PATCH)
    assert p["ok"], p
    s = p["preview"]
    assert s["grid"] == "10 x 10 x 6" and s["elapsed_s"] < 60 and s["maps"] == "zone"
    assert {c["key"] for c in s["changes"]} == {"layers", "petro.vsh_cutoff"}
    nums = {n["key"]: n for n in s["numbers"]}
    assert nums["cells"]["change"] == 100 and nums["at_wells_rms.ntg"]["new"] is not None
    assert set(s["findings"]) == {"fixed", "still", "new"}
    # nothing registered, no version
    con = db.connect(cfg.database)
    assert con.execute("SELECT COUNT(*) FROM geo_model_versions").fetchone()[0] == 1
    assert (pv.get(con, s["preview_id"])["folder"])
    # refusals: patch keys, no change
    assert tool(geo, "preview_geo_change", base_version_id=v1, spec_patch={"surfaces": []})["code"] == "INVALID_INPUT"
    assert tool(geo, "preview_geo_change", base_version_id=v1, spec_patch={"petro": {"vsh_cutoff": 0.5}})["code"] == \
        "INVALID_INPUT"
    # the agent can look at it like a version
    v = tool(geo, "render_review_view", spec={"fig": "diff_map", "preview_id": s["preview_id"], "base_version_id": v1,
                                             "kw": "NTG", "zone": "TopRes"})
    assert v["ok"] and set(v["at_wells"]) == {"A", "B", "C", "D"}, v
    # a proposal carries its preview, only of the same patch and base
    rv = DataService("reviewer", cfg)
    fid = rv.add_review_comment(kind="geo", version_id=v1, comment="NTG at B looks low")["feedback"]["id"]
    bad = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"layers": [4, 2]},
               rationale="x", preview_id=s["preview_id"])
    assert bad["code"] == "INVALID_INPUT" and "another version or patch" in bad["message"]
    pr = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch=PATCH, rationale="as previewed",
              preview_id=s["preview_id"])["proposal"]
    assert pr["preview"]["preview_id"] == s["preview_id"]
    # the build from it gives the preview's arrays exactly: a preview is what will be built
    assert tool(rv, "decide_geo_proposal", proposal_id=pr["id"], action="accept")["ok"]
    b = build_from_proposal(cfg, pr["id"])
    assert b["ok"], b
    from opmagent.geo import review_views as views
    built, prev = views.load_version(cfg, con, b["version_id"]), pv.load(cfg, con, s["preview_id"])
    for kw in ("PORO", "PERMX", "NTG"):
        assert np.allclose(built["arrays"][kw], prev["arrays"][kw], rtol=1e-5), kw


def test_engineer_previews_and_requests(cfg, v1):
    app = ReviewApp(cfg, v1, token="T")
    geo = DataService("geo", cfg)
    st, m = get(app, f"/api/meta?version_id={v1}")
    assert m["can_preview"] and m["spec"]["layers"] == [3, 2]
    assert post(app, "/api/previews", {"base_version_id": v1, "spec_patch": PATCH}, token="no")[0] == 403
    st, r = post(app, "/api/previews", {"base_version_id": v1, "spec_patch": {"petro": {"kv_kh": 0.05}}})
    assert st == 200, r
    pid = r["preview"]["preview_id"]
    nums = {n["key"]: n for n in r["preview"]["numbers"]}
    assert nums["cells"]["change"] == 0 and r["preview"]["maps"] == "layer"
    st, v = post(app, "/api/view", {"spec": {"fig": "layer_map", "preview_id": pid, "kw": "PERMZ", "layer": 2}})
    assert st == 200 and v["spec"] == {"fig": "layer_map", "preview_id": pid, "kw": "PERMZ", "log": True, "layer": 2}
    st, v = post(app, "/api/view", {"spec": {"fig": "diff_map", "preview_id": pid, "base_version_id": v1, "kw": "PERMZ",
                                             "layer": 2}})
    assert st == 200 and "preview #" in v["figure"]["layout"]["title"]["text"]
    st, loc = post(app, "/api/locate", {"spec": {"fig": "layer_map", "preview_id": pid, "kw": "PERMZ", "layer": 2},
                                        "role": "layer", "point": {"x": 1700, "y": 400}})
    assert loc["anchor"]["cell"] == [9, 3, 2] and loc["describe"]["cell"]["values"]["PERMZ"] > 0

    # an agent proposal waits; the engineer's own change supersedes it and is accepted at once
    fid = post(app, "/api/comments", {"comment": "kv/kh 0.1 is too high for these shales", "anchor": {"layer": 2}})[1]["feedback"]["id"]
    ap = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"petro": {"kv_kh": 0.08}},
              rationale="a little lower")["proposal"]
    assert post(app, "/api/requests", {"preview_id": pid, "feedback_ids": [fid], "rationale": ""})[0] == 400
    st, r = post(app, "/api/requests", {"preview_id": pid, "feedback_ids": [fid], "rationale": "0.05, from the core data"})
    assert st == 200, r
    p = r["proposal"]
    assert p["status"] == "accepted" and p["author_role"] == "reviewer" and p["reviewer"] == p["author"]
    assert proposals.get(app.con, ap["id"])["status"] == "superseded"
    t = review.thread(app.con, fid)
    assert [m["kind"] for m in t["messages"]][-2:] == ["proposal", "accepted"] and t["waiting_on"] == "modeler"
    # the agent hears it and builds it
    w = tool(geo, "await_review_activity", version_id=v1, timeout_s=3)
    assert w["event"] == "waiting_on_you" and w["threads"][0]["last"]["kind"] == "accepted"
    b = build_from_proposal(cfg, p["id"])
    assert b["ok"] and any("by the engineer" in a for a in b["assumptions"]), b
    # without a thread: the rationale becomes a general comment first
    pid2 = post(app, "/api/previews", {"base_version_id": b["version_id"], "spec_patch": {"layers": [3, 3]}})[1]["preview"]["preview_id"]
    st, r = post(app, "/api/requests", {"preview_id": pid2, "rationale": "MidRes needs a third layer"})
    assert st == 200, r
    f = review.get(app.con, r["proposal"]["feedback_ids"][0])
    assert f["comment"] == "MidRes needs a third layer" and f["label"] == "general" and f["version_id"] == b["version_id"]
    # only the reviewer requests; the agent never accepts
    assert "request_geo_change" not in TOOLS["geo"] and "request_geo_change" in TOOLS["reviewer"]


def test_previews_are_pruned(cfg, v1):
    geo = DataService("geo", cfg)
    s = tool(geo, "preview_geo_change", base_version_id=v1, spec_patch={"petro": {"kv_kh": 0.2}})["preview"]
    con = db.connect(cfg.database)
    folder = pv.get(con, s["preview_id"])["folder"]
    con.execute("UPDATE review_previews SET created_at = datetime('now', '-30 days') WHERE id = ?", (s["preview_id"],))
    assert pv.prune(replace(cfg, review_preview_keep_days=7), con) == 1
    from pathlib import Path
    assert not Path(folder).exists()
    with pytest.raises(Exception, match="removed"):
        pv.load(cfg, con, s["preview_id"])
    assert pv.get(con, s["preview_id"])["summary"]["preview_id"] == s["preview_id"]    # the summary stays


def test_preview_from_a_study(cfg):
    from opmagent.cli import _study_build
    from opmagent.core import studies
    from tests.study_data import load_raw
    con = db.connect(cfg.database)
    studies.init_study(con, cfg.studies_dir, "WI", "what-if", years=1, start_date="2026-01-01")
    assert load_raw(con, "WI")["all_ready"]
    studies.complete_task(con, "WI", "data", {})
    g = _study_build(cfg, con, "geo", "WI")
    assert g["ok"], g
    base = proposals.base_spec(con, "geo", g["version_id"])
    s = tool(DataService("geo", cfg), "preview_geo_change", base_version_id=g["version_id"],
             spec_patch={"layers": [n + 1 for n in base["layers"]]})
    assert s["ok"], s
    assert s["preview"]["grid"].endswith(f"x {sum(base['layers']) + len(base['layers'])}")


def test_model_without_payload_in_the_workbench(cfg):
    """hello's geo model has no QC payload (no tops or logs): the workbench reads it from the database, on cell
    indices; well, surface and log views don't apply, and it can't be previewed."""
    from opmagent.hello.run import hello, latest_demo
    hello(cfg, log=lambda *_: None)
    vid = latest_demo(db.connect(cfg.database))["geo"]["version_id"]
    app = ReviewApp(cfg, vid, token="T")
    st, m = get(app, f"/api/meta?version_id={vid}")
    assert m["db_only"] and m["views"] == ["layer_map", "section", "distributions", "diff_map"] and not m["can_preview"]
    assert m["wells"] == [] and m["grid"] == [10, 10, 3]
    assert post(app, "/api/view", {"spec": {"fig": "surfaces"}})[0] == 400
    st, v = post(app, "/api/view", {"spec": {"fig": "section", "kw": "PORO", "along": "x", "cell": [1, 4]}})
    assert st == 200 and "by layer" in v["figure"]["layout"]["title"]["text"]
    st, loc = post(app, "/api/locate", {"spec": {"fig": "section", "kw": "PORO", "along": "x", "cell": [1, 4]},
                                        "role": "section", "point": {"x": 6, "y": 2}})
    assert loc["anchor"] == {"cell": [6, 4, 2], "kw": "PORO"} and loc["describe"]["cell"]["top_tvd"] is None
    st, r = post(app, "/api/comments", {"comment": "uniform porosity: is that the deck's?", "anchor": loc["anchor"],
                                        "view": {"fig": "section", "kw": "PORO", "along": "x", "cell": [1, 4]}})
    assert st == 200, r
    ctx = tool(DataService("geo", cfg), "review_context", feedback_id=r["feedback"]["id"])
    assert ctx["ok"] and ctx["at_anchor"]["cell"]["values"]["PORO"] > 0 and "section" in ctx["view"]["shows"], ctx
    assert tool(DataService("geo", cfg), "preview_geo_change", base_version_id=vid,
                spec_patch={"layers": [4]})["code"] == "INVALID_INPUT"
