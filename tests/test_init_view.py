"""Initialisation view of a deck: the dynamic model at time zero (initial state, fluids in place) for ResInsight."""
import shutil

import pytest

from opmagent.core import db
from opmagent.deck.init_view import build_init_view, init_case, init_deck_text

pytestmark = pytest.mark.skipif(not shutil.which("flow"), reason="needs the OPM Flow binary")


def hello_deck(cfg):
    from opmagent.hello.run import hello
    from tests.test_deck_hello import approve_hello
    hello(cfg, log=lambda *_: None)
    approve_hello(cfg)
    return hello(cfg, log=lambda *_: None)["deck"]


def test_init_deck_replaces_summary_and_schedule():
    text = "RUNSPEC\nDIMENS\n 1 1 1 /\nSUMMARY\nFOPR\nSCHEDULE\nWELSPECS\n 'P' 'G' 1 1 1* OIL /\n/\nTSTEP\n 100 /\nEND\n"
    out = init_deck_text(text)
    assert "WELSPECS" not in out and "FOPR" not in out and "TSTEP\n 0.0001 /" in out
    assert out.index("DIMENS") < out.index("\nSUMMARY\n") < out.index("FOIP") < out.index("\nSCHEDULE\n")
    assert "Not for simulation" in out


def test_hello_deck_initialises(cfg):
    d = hello_deck(cfg)
    iv = d["init_view"]                                  # made by the deck build
    assert iv["in_place"]["FOIP"] > 0 and iv["units"]["FOIP"] == "SM3"
    assert iv["hydrocarbon_pv_fraction"] == pytest.approx(0.8, abs=1e-3)   # Swc 0.2, OWC below the grid
    assert [c["status"] for c in iv["checks"]] == ["pass"]
    con = db.connect(cfg.database)
    assert init_case(cfg, con, d["deck_id"]) is not None
    assert "results open --deck" in d["spoken_summary"]
    # the run table is untouched: the view is not a run
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    v = build_init_view(cfg, con, d["deck_id"])          # made again on demand, same numbers
    assert v["in_place"]["FOIP"] == pytest.approx(iv["in_place"]["FOIP"], rel=1e-9)


def test_results_open_and_mcp(cfg):
    from opmagent.servers.data_server import TOOLS, DataService, wrap
    from opmagent.viewer import find_case
    d = hello_deck(cfg)
    egrid, where = find_case(cfg, deck=d["deck_id"])
    assert egrid is not None and egrid.suffix == ".EGRID" and "time zero" in where
    assert {"init_view"} <= set(TOOLS["deck"]) & set(TOOLS["run"]) & set(TOOLS["view"]) & set(TOOLS["reviewer"])
    r = wrap(DataService("run", cfg), "init_view")(deck_id=d["deck_id"])
    assert r["ok"] and r["in_place"]["FOIP"] > 0, r
