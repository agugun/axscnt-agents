"""The review loops, both sides (testing/geo_review_loop.sh; see testing/geo-review.md).

The engineer's side goes through the running workbench over HTTP, with its token, as the browser does. The agent's
side goes through its profile's tools (geo, res or well), as the agent calls them, and the builds through the CLI.

  python geo_review_check.py loop <workbench url> <geo version id>     the geo loop
  python geo_review_check.py inputs <dir>                              res spec and well inputs on the geo field
  python geo_review_check.py res <workbench url> <res version id>      the res loop
  python geo_review_check.py well <workbench url> <well version id>    the well review (no proposals)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from opmagent.core import config as cfgmod
from opmagent.servers.data_server import DataService, wrap

FAILS = 0


def say(ok: bool, what: str, detail: str = "") -> bool:
    global FAILS
    print(("\033[32mPASS\033[0m " if ok else "\033[31mFAIL\033[0m ") + what + (f"\n     {detail}" if detail else ""))
    FAILS += not ok
    return ok


class Engineer:
    def __init__(self, url: str):
        u = urlparse(url)
        self.base, self.token = f"{u.scheme}://{u.netloc}", u.fragment.split("token=", 1)[1]

    def call(self, path: str, body=None, token=True):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Review-Token": self.token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def loop(url: str, v1: int) -> None:
    eng = Engineer(url)
    geo = DataService("geo", cfgmod.load(os.environ.get("OPMAGENT_CONFIG")))
    agent = lambda name, **kw: wrap(geo, name)(**kw)  # noqa: E731

    st, m = eng.call(f"/api/meta?version_id={v1}")
    say(st == 200, f"workbench answers: {m.get('model_name')} v{m.get('version')}, wells {m.get('wells')}")
    st, r = eng.call("/api/comments", {"comment": "x"}, token=False)
    say(st == 403, "a write without the token is refused")

    # the engineer clicks a cell in B's column on layer 2, and asks
    st, loc = eng.call("/api/locate", {"spec": {"fig": "layer_map", "version_id": v1, "kw": "PORO", "layer": 2},
                                       "role": "layer", "point": {"x": 1700, "y": 400}})
    anchor = loc.get("anchor", {})
    say(anchor.get("well") == "B", f"the click anchors {loc.get('label')}")
    st, r = eng.call("/api/comments", {"version_id": v1, "comment": "Is B's layer 2 porosity from the logs or the "
                                       "kriging? It looks high.", "severity": "question", "anchor": anchor,
                                       "view": {"fig": "layer_map", "kw": "PORO", "layer": 2}})
    fid = r.get("feedback", {}).get("id")
    say(st == 200 and fid, f"comment #{fid} pinned on {r.get('feedback', {}).get('label')}")

    # the agent listens: the comment wakes it, it reads what is there and replies with a view
    w = agent("await_review_activity", version_id=v1, timeout_s=10)
    say(w.get("event") == "waiting_on_you" and [t["id"] for t in w["threads"]] == [fid], "the waiting agent hears it")
    ctx = agent("review_context", feedback_id=fid)
    phi = ctx["at_anchor"]["cell"]["values"]["PORO"]
    say(ctx["ok"] and 0 < phi < 0.4, f"review_context: PORO {phi} at {ctx['feedback']['label']}")
    agent("reply_review", feedback_id=fid, body=f"From the logs: the cell holds {phi:.3f} and B's upscaled layer-2 "
          "value is the same; the 0.5 Vsh cut-off keeps a shaly interval.", views=[{"fig": "well", "well": "B"}])
    st, s = eng.call(f"/api/session?version_id={v1}")
    say(s.get("listening") is True and s.get("waiting_on_you") == 1, "the workbench shows the agent listening, "
        "and the thread waiting on the engineer")

    # the engineer answers while the agent waits
    got = {}
    th = threading.Thread(target=lambda: got.update(agent("await_review_activity", version_id=v1, timeout_s=30)))
    th.start()
    time.sleep(1)
    eng.call("/api/messages", {"feedback_id": fid, "body": "Try a 0.4 cut-off, and give TopRes 4 layers."})
    th.join(60)
    say(got.get("event") == "waiting_on_you", "the agent hears the reply")

    # the agent proposes; the engineer accepts; the agent builds it from the CLI
    p = agent("propose_geo_change", base_version_id=v1, feedback_ids=[fid],
              spec_patch={"petro": {"vsh_cutoff": 0.4}, "layers": [4, 2]}, rationale="as asked",
              expected="B layer 2 PORO about the same; more NTG contrast")
    pid = p.get("proposal", {}).get("id")
    say(p.get("ok"), f"proposal #{pid}: {p.get('spoken_summary')}")
    out = subprocess.run(["opmagent", "geo", "build", "--from-proposal", str(pid)], capture_output=True, text=True)
    say(out.returncode != 0 and "only an accepted proposal" in out.stdout, "an unaccepted proposal isn't built")
    st, r = eng.call("/api/proposals/decide", {"proposal_id": pid, "action": "accept"})
    say(st == 200 and r["proposal"]["status"] == "accepted", "the engineer accepts it in the workbench")
    out = subprocess.run(["opmagent", "geo", "build", "--from-proposal", str(pid)], capture_output=True, text=True)
    b = json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}
    v2 = b.get("version_id")
    say(b.get("ok") and b.get("grid", "").endswith("x 6"), f"built v{b.get('version')} ({b.get('grid')}) from the "
        "proposal", out.stderr[-500:] if not b.get("ok") else "")
    if not v2:
        return

    # the diff, from both sides
    d = agent("review_diff", version_id=v2)["diff"]
    keys = {s["key"] for s in d["spec"]}
    say(d["base_version_id"] == v1 and keys == {"petro.vsh_cutoff", "layers"} and d["maps"] == "zone",
        f"review_diff: v{d['version']} against v{d['base_version']}, spec {sorted(keys)}, maps by {d['maps']}")
    st, r = eng.call(f"/api/diff?version_id={v2}")
    say(st == 200 and [t["id"] for t in r["diff"]["threads"]] == [fid], "the workbench diff lists the comment it answers")
    st, r = eng.call("/api/view", {"spec": {"fig": "diff_map", "version_id": v2, "base_version_id": v1, "kw": "NTG",
                                            "zone": "TopRes"}})
    say(st == 200, "the NTG difference map draws (zone means)")

    # what-if: the agent previews a change; the engineer previews their own and asks the agent to build it
    pa = agent("preview_geo_change", base_version_id=v2, spec_patch={"layers": [4, 3]})
    say(pa.get("ok") and pa["preview"]["grid"].endswith("x 7"), f"the agent's preview: {pa.get('spoken_summary')}")
    st, r = eng.call("/api/previews", {"base_version_id": v2, "spec_patch": {"petro": {"kv_kh": 0.05}}})
    pv = r.get("preview", {})
    moved = {n["key"]: n for n in pv.get("numbers", []) if n["change"]}
    say(st == 200 and set(moved) == {"mean_PERMZ"}, f"the engineer's preview (kv/kh 0.05, {pv.get('elapsed_s')} s) "
        f"changes only {sorted(moved)}")
    st, r = eng.call("/api/requests", {"preview_id": pv.get("preview_id"), "rationale": "kv/kh 0.05 from the core data"})
    rp = r.get("proposal", {})
    say(st == 200 and rp.get("status") == "accepted" and rp.get("author_role") == "reviewer",
        f"the engineer's own change is proposal #{rp.get('id')}, accepted at once")
    w = agent("await_review_activity", version_id=v2, timeout_s=10)
    say(w.get("event") == "waiting_on_you" and any(t["last"]["kind"] == "accepted" for t in w["threads"]),
        "the agent hears it")
    out = subprocess.run(["opmagent", "geo", "build", "--from-proposal", str(rp.get("id"))], capture_output=True, text=True)
    b3 = json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}
    st, d3 = eng.call(f"/api/diff?version_id={b3.get('version_id')}")
    m3 = {n["key"]: n for n in d3.get("diff", {}).get("numbers", []) if n["change"]}
    say(b3.get("ok") and set(m3) == {"mean_PERMZ"} and m3["mean_PERMZ"]["new"] == moved["mean_PERMZ"]["new"],
        f"v{b3.get('version')} built from it is what the preview showed (mean PERMZ {m3.get('mean_PERMZ', {}).get('new')})")
    for fid3 in rp.get("feedback_ids", []):
        agent("answer_review_feedback", feedback_id=fid3, response=f"v{b3.get('version')}: kv/kh 0.05",
              status="addressed", addressed_in_version_id=b3.get("version_id"))

    # the agent answers; the engineer ends the session
    a = agent("answer_review_feedback", feedback_id=fid, response=f"v{b['version']}: Vsh 0.4, TopRes in 4 layers",
              status="addressed", addressed_in_version_id=v2)
    say(a.get("ok"), "the agent answers the thread with the new version")
    got.clear()
    th = threading.Thread(target=lambda: got.update(agent("await_review_activity", version_id=v2, timeout_s=30)))
    th.start()
    time.sleep(1)
    eng.call("/api/session/end", {"version_id": v2})
    th.join(60)
    say(got.get("event") == "session_ended", "ending the session in the workbench releases the agent")
    print(f"\nOpen the workbench to look at it: {url.replace(f'version_id={v1}', f'version_id={v2}&diff={v1}')}")


def inputs(d: str) -> None:
    """A res spec (two rock types by zone, OWC 2100 m) and well inputs (a producer through well B, an injector through
    well A, six months of history) on the geo scenario's field."""
    from pathlib import Path

    import pandas as pd
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    (p / "res.json").write_text(json.dumps({
        "scal": [{"satnum": 1, "swc": 0.2, "sorw": 0.2}, {"satnum": 2, "swc": 0.25, "sorw": 0.25, "nw": 3}],
        "contacts": {"datum_depth": 2000.0, "datum_pressure": 200.0, "owc_depth": 2100.0},
        "regions": {"SATNUM": {"by": "zone", "values": {"TopRes": 1, "MidRes": 2}}}}, indent=1))
    heads = {"P1": ("PRODUCER", "OIL", 1700.0, 400.0), "I1": ("INJECTOR", "WATER", 300.0, 300.0)}
    pd.DataFrame([{"well_name": w, "type": t, "phase": ph, "x": x, "y": y, "ref_depth": 2000}
                  for w, (t, ph, x, y) in heads.items()]).to_csv(p / "wells.csv", index=False)
    pd.DataFrame([{"well_name": w, "md": md, "inc": 0, "azi": 0, "x0": x, "y0": y}
                  for w, (_, _, x, y) in heads.items() for md in (0, 1900, 2100)]).to_csv(p / "surveys.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-01-01", "top_md": 2020, "bottom_md": 2045},
                  {"well_name": "I1", "date": "2026-01-01", "top_md": 2005, "bottom_md": 2045}]).to_csv(p / "perfs.csv", index=False)
    days = pd.date_range("2026-01-01", "2026-06-30")
    pd.concat([pd.DataFrame({"well_name": "P1", "date": days, "oil": 400.0, "water": 20.0, "gas": 32000.0, "bhp": 180.0}),
               pd.DataFrame({"well_name": "I1", "date": days, "water_inj": 450.0})]).to_csv(p / "history.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-07-01", "control": "ORAT", "rate": 400, "bhp": 100},
                  {"well_name": "I1", "date": "2026-07-01", "control": "RATE", "rate": 450, "bhp": 350}]
                 ).to_csv(p / "controls.csv", index=False)
    print(f"wrote {p}/res.json, wells.csv, surveys.csv, perfs.csv, history.csv, controls.csv")


def _service(profile: str):
    return DataService(profile, cfgmod.load(os.environ.get("OPMAGENT_CONFIG")))


def _build(*args) -> dict:
    out = subprocess.run(["opmagent", *args], capture_output=True, text=True)
    return json.loads(out.stdout) if out.stdout.strip().startswith("{") else {"ok": False, "message": out.stderr[-500:]}


def res(url: str, v1: int) -> None:
    eng = Engineer(url)
    svc = _service("res")
    agent = lambda name, **kw: wrap(svc, name)(**kw)  # noqa: E731

    st, m = eng.call(f"/api/meta?version_id={v1}")
    say(st == 200 and m.get("kind") == "res" and m.get("can_preview") and m.get("satnums") == [1, 2],
        f"res workbench: {m.get('model_name')} v{m.get('version')}, views {m.get('views')}")
    st, loc = eng.call("/api/locate", {"spec": {"fig": "contacts", "version_id": v1}, "role": "contact",
                                       "point": {"eqlnum": 1, "contact": "OWC"}})
    pv_oil = loc.get("describe", {}).get("eqlnum", {}).get("pv_oil")
    say(loc.get("label") == "EQLNUM 1 OWC" and (pv_oil or 0) > 0, f"the click anchors {loc.get('label')} (oil PV {pv_oil})")
    st, r = eng.call("/api/comments", {"version_id": v1, "comment": "OWC 2100 looks deep: the RFT says about 2060",
                                       "severity": "question", "anchor": loc.get("anchor"), "view": {"fig": "contacts"}})
    fid = r.get("feedback", {}).get("id")
    say(st == 200 and fid, f"comment #{fid} pinned on {r.get('feedback', {}).get('label')}")

    w = agent("await_review_activity", version_id=v1, timeout_s=10)
    say(w.get("event") == "waiting_on_you" and [t["id"] for t in w["threads"]] == [fid], "the waiting res agent hears it")
    ctx = agent("review_context", feedback_id=fid)
    say(ctx.get("ok") and ctx["at_anchor"]["eqlnum"]["owc_depth"] == 2100, "review_context: the contacts and pore volumes")
    p = agent("preview_res_change", base_version_id=v1, spec_patch={"contacts": {"owc_depth": 2060}})
    moved = {n["key"]: n for n in p.get("preview", {}).get("numbers", []) if n["change"]}
    oil, water = moved.get("eqlnum.1.pv_oil", {}), moved.get("eqlnum.1.pv_water", {})
    say(p.get("ok") and oil.get("change", 0) < 0 and water.get("change") == -oil.get("change"),
        f"the agent's preview of OWC 2060 ({p.get('preview', {}).get('elapsed_s')} s, no Flow run): oil PV "
        f"{oil.get('change')} moves to water")
    pid = p.get("preview", {}).get("preview_id")
    agent("reply_review", feedback_id=fid, body="The RFT fits 2060; the oil pore volume drops by the amount shown.",
          views=[{"fig": "contacts", "preview_id": pid, "base_version_id": v1}])
    pr = agent("propose_res_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"contacts": {"owc_depth": 2060}},
               rationale="RFT pressure gradients", preview_id=pid)
    prop = pr.get("proposal", {}).get("id")
    say(pr.get("ok") and pr["proposal"]["preview"]["preview_id"] == pid, f"proposal #{prop} carries its preview")
    b = _build("res", "build", "--from-proposal", str(prop))
    say(not b.get("ok") and "only an accepted proposal" in str(b.get("message")), "an unaccepted proposal isn't built")
    st, r = eng.call("/api/proposals/decide", {"proposal_id": prop, "action": "accept"})
    say(st == 200 and r["proposal"]["status"] == "accepted", "the engineer accepts it in the workbench")
    b = _build("res", "build", "--from-proposal", str(prop))
    v2 = b.get("version_id")
    say(b.get("ok") and b.get("status") == "qc_ready", f"res v{b.get('version')} built from it with `opmagent res build "
        "--from-proposal`", "" if b.get("ok") else str(b.get("message")))
    if not v2:
        return
    st, d = eng.call(f"/api/diff?version_id={v2}")
    d = d.get("diff", {})
    keys = {n["key"] for n in d.get("numbers", []) if n["change"]}
    say(d.get("base_version_id") == v1 and keys == {"eqlnum.1.owc_depth", "eqlnum.1.pv_oil", "eqlnum.1.pv_water"}
        and [t["id"] for t in d.get("threads", [])] == [fid], f"the diff: {sorted(keys)}, and the comment it answers")
    agent("answer_review_feedback", feedback_id=fid, response=f"v{b['version']}: OWC 2060", status="addressed",
          addressed_in_version_id=v2)

    # the engineer's own SCAL change on v2, previewed over the base curves, requested; the agent builds it
    patch = {"scal": [{"satnum": 1, "swc": 0.2, "sorw": 0.2}, {"satnum": 2, "swc": 0.25, "sorw": 0.25, "nw": 2.5}]}
    st, r = eng.call("/api/previews", {"base_version_id": v2, "spec_patch": patch})
    pv2 = r.get("preview", {})
    say(st == 200 and [c["key"] for c in pv2.get("changes", [])] == ["scal.2.nw"],
        f"the engineer's preview reads {[c['key'] for c in pv2.get('changes', [])]}")
    st, kr = eng.call("/api/view", {"spec": {"fig": "kr", "preview_id": pv2.get("preview_id"), "base_version_id": v2}})
    say(st == 200 and any(f"(v{b['version']})" in (t.get("name") or "") for t in kr["figure"]["data"]),
        "its relative permeability is drawn over the base's dotted curves")
    st, r = eng.call("/api/requests", {"preview_id": pv2.get("preview_id"), "rationale": "SCAL lab: nw 2.5 for SATNUM 2"})
    say(st == 200 and r["proposal"]["status"] == "accepted", f"the engineer's own change is proposal #{r['proposal']['id']}")
    w = agent("await_review_activity", version_id=v2, timeout_s=10)
    say(w.get("event") == "waiting_on_you", "the res agent hears it")
    b3 = _build("res", "build", "--from-proposal", str(r["proposal"]["id"]))
    say(b3.get("ok") and any("by the engineer" in a for a in b3.get("assumptions", [])), f"res v{b3.get('version')} built from it")
    for f in r["proposal"]["feedback_ids"]:
        agent("answer_review_feedback", feedback_id=f, response=f"v{b3.get('version')}: nw 2.5", status="addressed",
              addressed_in_version_id=b3.get("version_id"))
    _end_session(eng, agent, b3.get("version_id") or v2, "res")


def well(url: str, vid: int) -> None:
    eng = Engineer(url)
    svc = _service("well")
    agent = lambda name, **kw: wrap(svc, name)(**kw)  # noqa: E731
    st, m = eng.call(f"/api/meta?version_id={vid}")
    say(st == 200 and m.get("kind") == "well" and not m.get("can_preview") and not m.get("can_propose"),
        f"well workbench: {m.get('model_name')} v{m.get('version')}, views {m.get('views')}, no proposals")
    st, v = eng.call("/api/view", {"spec": {"fig": "well_section", "version_id": vid, "well": "P1"}})
    perf = next((t["meta"] for t in v.get("figure", {}).get("data", []) if t.get("meta", {}).get("role") == "perforation"), {})
    st, loc = eng.call("/api/locate", {"spec": {"fig": "well_section", "version_id": vid, "well": "P1"},
                                       "role": "perforation", "point": perf})
    cells = loc.get("describe", {}).get("perforation_cells") or []
    say(loc.get("anchor", {}).get("perforation") and cells and all(c["k"] > 0 for c in cells),
        f"a click on P1's perforation anchors {loc.get('label')}, in {len(cells)} cell(s) of the geo grid")
    st, r = eng.call("/api/comments", {"version_id": vid, "comment": "Is P1 perforated into the MidRes too?",
                                       "severity": "question", "anchor": loc.get("anchor"),
                                       "view": {"fig": "well_section", "well": "P1"}})
    fid = r.get("feedback", {}).get("id")
    say(st == 200 and fid, f"comment #{fid} pinned on {r.get('feedback', {}).get('label')}")
    w = agent("await_review_activity", version_id=vid, timeout_s=10)
    say(w.get("event") == "waiting_on_you" and [t["id"] for t in w["threads"]] == [fid], "the waiting well agent hears it")
    ctx = agent("review_context", feedback_id=fid)
    layers = sorted({c["k"] for c in ctx.get("at_anchor", {}).get("perforation_cells", [])})
    say(ctx.get("ok") and layers, f"review_context: the perforation's cells are in layers {layers}")
    agent("reply_review", feedback_id=fid, body=f"P1 is open in layers {layers}; MidRes starts at layer 4.",
          views=[{"fig": "perforated_cells"}])
    st, s = eng.call(f"/api/session?version_id={vid}")
    say(s.get("waiting_on_you") == 1, "the reply waits on the engineer")
    st, r = eng.call("/api/previews", {"base_version_id": vid, "spec_patch": {"x": 1}})
    say(st == 400, "no previews for a well model (its changes are data edits)")
    agent("answer_review_feedback", feedback_id=fid, response="as built: no change needed", status="wontfix")
    _end_session(eng, agent, vid, "well")


def _end_session(eng, agent, vid, kind):
    got = {}
    th = threading.Thread(target=lambda: got.update(agent("await_review_activity", version_id=vid, timeout_s=30)))
    th.start()
    time.sleep(1)
    eng.call("/api/session/end", {"version_id": vid})
    th.join(60)
    say(got.get("event") == "session_ended", f"ending the session releases the {kind} agent")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "inputs" and len(sys.argv) == 3:
        inputs(sys.argv[2])
        return
    if cmd not in ("loop", "res", "well") or len(sys.argv) != 4:
        raise SystemExit(__doc__)
    {"loop": loop, "res": res, "well": well}[cmd](sys.argv[2], int(sys.argv[3]))
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
