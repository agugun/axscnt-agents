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


@pytest.mark.skipif(not (OPM_TESTS and os.environ.get("OPMAGENT_SLOW")),
                    reason="Norne runs Flow twice (~10 min): set OPM_TESTS_DIR and OPMAGENT_SLOW=1")
def test_norne_roundtrip_is_bit_exact(cfg):
    # injectors declared 'OIL', hysteresis end-points left defaulted, arithmetic-only values (SGU = 1 - SWL)
    r, v = roundtrip(cfg, str(Path(OPM_TESTS) / "norne/NORNE_ATW2013.DATA"), "NORNE")
    assert not r["validator"]["well"]["errors"]
    dyn = v["comparison"]["levels"][2]
    assert dyn["vectors_compared"] > 2000
    assert dyn["worst"][0][1] == 0.0, dyn["worst"][:5]   # Flow is deterministic: an exact deck gives identical runs


def test_injector_declared_oil_in_welspecs(cfg, tmp_path):
    # Norne declares its water injectors with preferred phase 'OIL' in WELSPECS; WCONINJE decides the type
    import re
    deck = Path(hello_deck(cfg)).read_text()
    deck, n = re.subn(r"(\n 'INJ1' [^\n]*)'WATER' /", r"\1'OIL' /", deck, count=1)
    assert n == 1
    p = tmp_path / "OILINJ.DATA"
    p.write_text(deck)
    r, v = roundtrip(cfg, str(p), "OILINJ")
    assert not r["validator"]["well"]["errors"]
    rv = DataService("reviewer", cfg)
    vid = next(m["version_id"] for m in rv.list_models()["models"] if m["model_name"] == "OILINJ_WELL")
    w = {n: (t, ph) for n, t, ph in rv.con.execute(
        "SELECT well_name, well_type, preferred_phase FROM wells WHERE version_id=?", (vid,))}
    assert w["INJ1"] == ("INJECTOR", "WATER")
    assert w["PROD1"][0] == "PRODUCER"


def test_endpoint_cells_left_unset():
    # Norne: COPY 'SWL' 'ISWL' only for layers 5-22; the other cells must stay defaulted (from the table)
    from opm.io.parser import Parser
    from opmagent.deck.importer import _assigned_cells
    deck = Parser().parse_string("""RUNSPEC
DIMENS
 2 2 3 /
GRID
PROPS
EQUALS
 'SWL' 0.1 /
/
COPY
 'SWL' 'ISWL' 1 2 1 2 2 3 /
/
BOX
 1 1 1 1 1 1 /
EQUALS
 'ISWU' 0.9 /
/
ENDBOX
EQUALS
 'ISWU' 0.8 2 2 1* 1* 3 3 /
/
""")
    m = _assigned_cells(deck, ["SWL", "ISWL", "ISWU"], 2, 2, 3)
    assert m["SWL"].all()
    assert m["ISWL"].tolist() == [False] * 4 + [True] * 8
    assert m["ISWU"].nonzero()[0].tolist() == [0, 9, 11]   # box cell (1,1,1); i=2 in layer 3


def test_exact_tokens_round_trip_through_opm():
    import numpy as np
    from opmagent.deck.importer import MD_TO_SI, _opm_read, exact_tokens
    perm_si = np.array([213.478149, 0.004 * 96.0, 2759.0376]) * MD_TO_SI
    tok, bad, read = exact_tokens(perm_si, MD_TO_SI)
    assert bad == 0 and np.array_equal(_opm_read(tok) * MD_TO_SI, perm_si) and np.array_equal(read, perm_si)
    # 0.22999999999999998 (= 0.15 + 0.08) is readable with the right digits; 0.9299999999999999 (= 1 - 0.07) is not
    tok, bad, _ = exact_tokens(np.array([0.15 + 0.08, 1 - 0.07]))
    assert _opm_read(tok[:1])[0] == 0.15 + 0.08 and bad == 1


def test_replay_makes_arithmetic_values_exact():
    # Norne: SGU = 1 - SWL through COPY/MULTIPLY/ADD; OPM cannot read 1 - 0.07 from text, so the ops are replayed
    import numpy as np
    from opm.io.parser import Parser
    from opmagent.deck.importer import _replay_ops
    deck = Parser().parse_string("""RUNSPEC
DIMENS
 2 1 1 /
GRID
PROPS
EQUALS
 'SWL' 0.07 /
/
COPY
 'SWL' 'SGU' /
/
MULTIPLY
 'SGU' -1 /
/
ADD
 'SGU' 1 /
/
""")
    swl = np.full(2, 0.07)
    sgu = 1 + swl * -1
    near = np.full(2, 0.93)          # what OPM reads from the closest decimal text
    assert not np.array_equal(near, sgu)
    written = {"SWL": (swl, swl, 1.0, None), "SGU": (near, sgu, 1.0, None)}
    ops, exact = _replay_ops(deck, written, {"SGU"}, 2, 1, 1, np.ones(2, dtype=bool))
    assert exact == {"SGU"}
    assert [t.split()[0] for _, t in ops] == ["COPY", "MULTIPLY", "ADD"]


def test_reimport_keeps_earlier_files(cfg):
    # a re-import must not overwrite the files an earlier (approved) version was reviewed with
    import hashlib
    deck = hello_deck(cfg)
    first, v1 = roundtrip(cfg, deck, "TWICE")
    second = import_to_drafts(cfg, deck, "TWICE")
    assert first["import_dir"] != second["import_dir"]
    con = DataService("reviewer", cfg).con
    rows = con.execute("SELECT path, sha256 FROM files WHERE path LIKE ?", (first["import_dir"] + "/%",)).fetchall()
    assert rows
    for path, sha in rows:
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha, path
    # the second import is not approved yet: verify still uses the first import's models and files
    v = verify(cfg, "TWICE")
    assert v["ok"] and v["report"].startswith(first["import_dir"])
    approve(cfg, "TWICE")
    v = verify(cfg, "TWICE")
    assert v["ok"] and v["report"].startswith(second["import_dir"])


def test_import_is_all_or_nothing(cfg, tmp_path):
    bad = tmp_path / "BAD.DATA"
    deck = Path(hello_deck(cfg)).read_text().replace("WELSPECS", "WELSPECS\n 'GHOST' 'FIELD' 1 2 2000 'OIL' /\n/\nWELSPECS", 1)
    bad.write_text(deck.replace("'PROD1' 10 10 1 3", "'NOWELL' 10 10 1 3"))
    with pytest.raises(Exception):
        import_to_drafts(cfg, str(bad), "BAD")
    assert not [m for m in DataService("reviewer", cfg).list_models()["models"] if m["model_name"].startswith("BAD_")]


def test_review_cli_refuses_decided_models_without_prompting(cfg, monkeypatch, capsys):
    import argparse
    from opmagent import cli
    r, _ = roundtrip(cfg, hello_deck(cfg), "CLIHI")          # all three CLIHI models are approved
    monkeypatch.setattr(cli.cfgmod, "load", lambda _path: cfg)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt for a decided model"))
    for rcmd in ("approve", "reject"):
        args = argparse.Namespace(config=None, rcmd=rcmd, kind="well", version_id=r["versions"]["well"], comment="x")
        with pytest.raises(SystemExit) as e:
            cli.cmd_review(args)
        assert "already approved" in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli.cmd_review(argparse.Namespace(config=None, rcmd="approve", kind="well", version_id=999, comment=None))
    assert "not found" in str(e.value)
