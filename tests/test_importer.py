"""Deck importer round trips: the hello deck always; SPE1/SPE9 if OPM_TESTS_DIR points to opm-tests."""
import os
from pathlib import Path

import pytest

from opmagent.deck.import_pipeline import import_to_drafts, verify
from opmagent.hello.run import hello, latest_demo
from opmagent.servers.data_server import DataService

pytest.importorskip("opm.simulators")


def approve(cfg, name):
    rv = DataService("reviewer", cfg)
    for p in rv.list_pending_reviews()["pending"]:
        if p["model_name"].startswith(name + "_"):
            rid = rv.get_qc_report(kind=p["kind"], version_id=p["version_id"])["qc_report"]["id"]
            assert rv.approve(kind=p["kind"], version_id=p["version_id"], qc_report_file_id=rid)["ok"]


def hello_deck(cfg):
    hello(cfg, log=lambda *_: None)
    approve_all = DataService("reviewer", cfg)
    for kind, m in latest_demo(approve_all.con).items():
        rid = approve_all.get_qc_report(kind=kind, version_id=m["version_id"])["qc_report"]["id"]
        approve_all.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid)
    return hello(cfg, log=lambda *_: None)["deck"]["path"]


def roundtrip(cfg, deck, name):
    r = import_to_drafts(cfg, deck, name)
    assert r["ok"], r
    approve(cfg, name)
    v = verify(cfg, name)
    assert v["ok"], v["spoken_summary"]
    return r, v


def test_hello_roundtrip(cfg):
    r, v = roundtrip(cfg, hello_deck(cfg), "HELLOIMP")
    assert r["schedule_mode"] == "model"
    assert v["comparison"]["levels"][2]["vectors_compared"] > 10


OPM_TESTS = os.environ.get("OPM_TESTS_DIR")


@pytest.mark.skipif(not OPM_TESTS, reason="set OPM_TESTS_DIR to an opm-tests checkout (tools/fetch_open_data.sh)")
@pytest.mark.parametrize("deck,name,mode", [("spe1/SPE1CASE1.DATA", "SPE1", "model"), ("spe9/SPE9.DATA", "SPE9", "verbatim")])
def test_open_decks_roundtrip(cfg, deck, name, mode):
    r, v = roundtrip(cfg, str(Path(OPM_TESTS) / deck), name)
    assert r["schedule_mode"] == mode


def test_import_is_all_or_nothing(cfg, tmp_path):
    bad = tmp_path / "BAD.DATA"
    deck = Path(hello_deck(cfg)).read_text().replace("WELSPECS", "WELSPECS\n 'GHOST' 'FIELD' 1 2 2000 'OIL' /\n/\nWELSPECS", 1)
    bad.write_text(deck.replace("'PROD1' 10 10 1 3", "'NOWELL' 10 10 1 3"))
    with pytest.raises(Exception):
        import_to_drafts(cfg, str(bad), "BAD")
    assert not [m for m in DataService("reviewer", cfg).list_models()["models"] if m["model_name"].startswith("BAD_")]
