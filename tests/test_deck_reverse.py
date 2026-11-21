"""The reverse process: the data manager reads a deck into a study's data; the modelers, deck builder and run agent
rebuild it through their usual steps; the study's run reproduces the source deck's results."""
import json
import shutil
import os
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("opm.simulators")
from opmagent.core import db, studies  # noqa: E402
from opmagent.data import deck_source, store  # noqa: E402
from opmagent.deck.importer import import_deck  # noqa: E402
from opmagent.servers.data_server import DataService, wrap  # noqa: E402
from opmagent.servers.sim_server import SimService  # noqa: E402
from opmagent.servers.sim_server import wrap as swrap  # noqa: E402
from tests.test_importer import OPM_TESTS, hello_deck  # noqa: E402


def reverse_study(cfg, deck: str, name: str) -> dict:
    """Every step through the profile of the agent that does it, as in a real study."""
    from opmagent.cli import _study_build
    cfg = replace(cfg, auto_approve=True)
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    con = db.connect(cfg.database)
    assert call("deck", "init_study", name=name, requirement=f"rebuild {Path(deck).name}")["ok"]
    call("data", "start_task", name=name)
    chk = call("data", "load_deck_data", name=name, deck_path=deck, check_only=True)
    assert chk["ok"] and not chk["loaded"] and not deck_source.has_deck(con, name)          # check: nothing kept
    m = call("data", "load_deck_data", name=name, deck_path=deck)
    assert m["ok"] and m["all_ready"], m.get("issues") or m
    assert call("data", "complete_task", name=name, result={})["unlocked"] == ["fluid", "geo", "well"]
    assert call("res", "deck_tables", name=name, kind="res")["row_counts"]   # SCAL, rock, contacts: the res model's
    for kind in ("fluid", "geo", "res", "well"):   # geo before res and well: both are on its grid
        call(kind, "start_task", name=name)
        r = _study_build(cfg, con, kind, name)
        assert r["ok"] and r["status"] == "approved", r
        assert call(kind, "complete_task", name=name, result={"version_id": r["version_id"]})["ok"]
    call("deck", "start_task", name=name)
    d = _study_build(cfg, con, "deck", name)
    assert d["ok"], d.get("validation_log")
    call("deck", "complete_task", name=name, result={"deck_id": d["deck_id"]})
    call("run", "start_task", name=name)
    sim = SimService(cfg)
    rid = swrap(sim, "run_simulation")(deck_id=d["deck_id"])["run_id"]
    assert swrap(sim, "wait")(run_id=rid, timeout_s=1200)["ok"]
    call("run", "complete_task", name=name, result={"run_id": rid})
    return deck_source.compare_with_source(cfg, con, name)


def test_hello_deck_rebuilt_through_a_study(cfg):
    deck = hello_deck(cfg)
    c = reverse_study(cfg, deck, "REV")
    assert c["ok"], c["spoken_summary"]
    assert c["comparison"]["levels"][2]["vectors_compared"] > 10
    con = db.connect(cfg.database)
    # the models record where their data came from; the rows are the importer's, exactly
    v = con.execute("SELECT spec_json, source_deck_file_id FROM geo_model_versions WHERE id = ?",
                    (studies.status(con, "REV")["tasks"][2]["result"]["version_id"],)).fetchone()
    assert json.loads(v["spec_json"])["data_source"]["study"] == "REV" and v["source_deck_file_id"]
    imp = import_deck(deck, cfg.file_store / "tmp_import")
    assert deck_source.deck_tables(con, "REV", "fluid")["fluid"]["pvto"] == \
        json.loads(json.dumps(imp["fluid"]["tables"]["pvto"], default=str))
    assert "compared" in [e["event"] for e in studies.events(con, "REV")]


def forward_deck(cfg) -> str:
    """A deck the agents built forward from raw data (corner-point grid, kriged arrays, completions from surveys)."""
    from opmagent.cli import _study_build
    from tests.study_data import load_raw
    cfg = replace(cfg, auto_approve=True)
    con = db.connect(cfg.database)
    studies.init_study(con, cfg.studies_dir, "FWD", "forward", years=1, start_date="2026-01-01")
    assert load_raw(con, "FWD")["all_ready"]
    studies.complete_task(con, "FWD", "data", {})
    for kind in ("geo", "res", "fluid", "well"):
        r = _study_build(cfg, con, kind, "FWD")
        studies.complete_task(con, "FWD", kind, {"version_id": r["version_id"]}, auto_approve=True)
    d = _study_build(cfg, con, "deck", "FWD")
    assert d["ok"], d.get("validation_log")
    return d["path"]


def test_forward_built_deck_reversed_reproduces_it(cfg):
    pytest.importorskip("lasio")
    c = reverse_study(cfg, forward_deck(cfg), "BACK")
    assert c["ok"], c["spoken_summary"]


def test_reload_keeps_earlier_files_and_integrity_applies(cfg):
    pytest.importorskip("lasio")
    con = db.connect(cfg.database)
    deck = forward_deck(cfg)
    studies.init_study(con, cfg.studies_dir, "REV2", "x")
    deck_source.load_deck(con, "REV2", deck)
    first = deck_source.deck_info(con, "REV2")["folder"]
    deck_source.load_deck(con, "REV2", deck)
    second = deck_source.deck_info(con, "REV2")["folder"]
    assert first != second and Path(first).is_dir()              # versions built from load-001 keep their files
    assert store.inventory(con, "REV2")["datasets"]["deck"]["rows"] > 0
    # a cell array edited after the load is refused when a modeler builds from it
    arr = next(x["data_file_id"] for x in deck_source.deck_info(con, "REV2")["props"] if "data_file_id" in x)
    path = Path(con.execute("SELECT path FROM data_files WHERE id = ?", (arr,)).fetchone()[0])
    assert path.parent == Path(second)
    path.write_text(path.read_text() + "\n")
    from opmagent.core.errors import ToolError
    with pytest.raises(ToolError, match="changed since it was loaded"):
        deck_source.build_model(cfg, con, "REV2", "geo", "REV2_Geo")


@pytest.mark.skipif(not OPM_TESTS, reason="set OPM_TESTS_DIR to an opm-tests checkout (tools/fetch_open_data.sh)")
@pytest.mark.parametrize("deck,name", [("spe1/SPE1CASE1.DATA", "RSPE1"), ("spe9/SPE9.DATA", "RSPE9")])
def test_open_decks_rebuilt_through_a_study(cfg, deck, name):
    c = reverse_study(cfg, str(Path(OPM_TESTS) / deck), name)
    assert c["ok"], c["spoken_summary"]


@pytest.mark.skipif(not (OPM_TESTS and os.environ.get("OPMAGENT_SLOW")),
                    reason="Norne runs Flow twice (~10 min): set OPM_TESTS_DIR and OPMAGENT_SLOW=1")
def test_norne_rebuilt_through_a_study(cfg):
    c = reverse_study(cfg, str(Path(OPM_TESTS) / "norne/NORNE_ATW2013.DATA"), "RNORNE")
    assert c["ok"], c["spoken_summary"]


def _models(cfg, con, call, name):
    from opmagent.cli import _study_build
    for kind in ("fluid", "geo", "res", "well"):
        call(kind, "start_task", name=name)
        r = _study_build(cfg, con, kind, name)
        assert r["ok"] and r["status"] == "approved", r
        assert call(kind, "complete_task", name=name, result={"version_id": r["version_id"]})["ok"]


def test_two_scenarios_input_reused_and_deck_the_same(cfg):
    """testing/spe9-reverse-loop.md on the hello deck: scenario 1 reads the deck and builds the models (views, no
    run); scenario 2 takes scenario 1's input, builds again, renders the deck, runs it. Same deck, same results."""
    from opmagent.cli import _study_build, _study_view
    deck = hello_deck(cfg)                       # approves the demo models itself: before auto-approve
    cfg = replace(cfg, auto_approve=True)
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    con = db.connect(cfg.database)
    # scenario 1
    call("deck", "init_study", name="S1", requirement="reverse")
    call("data", "start_task", name="S1")
    assert call("data", "load_deck_data", name="S1", deck_path=deck)["all_ready"]
    call("data", "complete_task", name="S1", result={})
    _models(cfg, con, call, "S1")
    views = _study_view(con, "S1", ["fluid", "geo", "res", "well"])
    assert views["ok"]
    for v in views["views"]:
        page = Path(v["qc_report"]).read_text()
        assert "plotly" in page.lower() and "(from deck)" in page
    # scenario 2: the input, not the deck
    call("deck", "init_study", name="S2", requirement="rebuild from S1")
    call("data", "start_task", name="S2")
    assert not call("geo", "load_study_input", name="S2", source_study="S1")["ok"]    # only the data manager
    m = call("data", "load_study_input", name="S2", source_study="S1")
    assert m["ok"] and m["all_ready"] and m["rows"] > 0, m
    assert deck_source.deck_tables(con, "S2") == deck_source.deck_tables(con, "S1")
    f1, f2 = (deck_source.deck_info(con, s)["folder"] for s in ("S1", "S2"))
    assert f1 != f2 and "/S2/" in f2                                                  # S2 has its own files
    call("data", "complete_task", name="S2", result={})
    _models(cfg, con, call, "S2")
    call("deck", "start_task", name="S2")
    d = _study_build(cfg, con, "deck", "S2")
    assert d["ok"], d.get("validation_log")
    call("deck", "complete_task", name="S2", result={"deck_id": d["deck_id"]})
    c = deck_source.compare_with_source(cfg, con, "S2", deck_only=True)                 # criterion 1: no run
    assert c["ok"] and c["deck"]["passed"] and "comparison" not in c, c["spoken_summary"]
    call("run", "start_task", name="S2")
    sim = SimService(cfg)
    rid = swrap(sim, "run_simulation")(deck_id=d["deck_id"])["run_id"]
    assert swrap(sim, "wait")(run_id=rid, timeout_s=600)["ok"]
    call("run", "complete_task", name="S2", result={"run_id": rid})
    c = deck_source.compare_with_source(cfg, con, "S2")                                 # criterion 2: the runs
    assert c["ok"] and c["comparison"]["passed"], c["spoken_summary"]


def test_deck_compare_finds_a_changed_number(cfg, tmp_path):
    from opmagent.deck.deck_compare import compare_decks
    deck = Path(hello_deck(cfg))
    assert compare_decks(deck, deck)["passed"]
    work = tmp_path / "changed"
    shutil.copytree(deck.parent, work)
    changed = work / deck.name
    text = changed.read_text()
    i = text.index("PVTW")
    j = text.index("/", i)
    rec = text[i:j].split()
    rec[2] = repr(float(rec[2]) * 1.01)                       # water Bw +1 %
    changed.write_text(text[:i] + " ".join(rec[:1]) + "\n " + " ".join(rec[1:]) + " " + text[j:])
    r = compare_decks(deck, changed)
    assert not r["passed"]
    tab = next(s for s in r["sections"] if s["section"] == "tables")
    assert not tab["passed"] and any("pvtw" in d for d in tab["differences"])


def test_input_copy_refuses_changed_files(cfg):
    pytest.importorskip("lasio")
    con = db.connect(cfg.database)
    deck = forward_deck(cfg)
    for s in ("C1", "C2"):
        studies.init_study(con, cfg.studies_dir, s, "x")
    deck_source.load_deck(con, "C1", deck)
    arr = next(x["data_file_id"] for x in deck_source.deck_info(con, "C1")["props"] if "data_file_id" in x)
    path = Path(con.execute("SELECT path FROM data_files WHERE id = ?", (arr,)).fetchone()[0])
    path.write_text(path.read_text() + "\n")
    from opmagent.core.errors import ToolError
    with pytest.raises(ToolError, match="changed since it was loaded"):
        deck_source.copy_input(con, "C2", "C1")
    assert not deck_source.has_deck(con, "C2")
