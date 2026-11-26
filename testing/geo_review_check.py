"""The geo review loop, both sides (testing/geo_review_loop.sh; see testing/geo-review.md).

The engineer's side goes through the running workbench over HTTP, with its token, as the browser does. The geo
agent's side goes through the geo profile's tools, as the agent calls them, and the build through the CLI.

  python geo_review_check.py loop <workbench url> <geo version id>
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


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "loop":
        raise SystemExit(__doc__)
    loop(sys.argv[2], int(sys.argv[3]))
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
