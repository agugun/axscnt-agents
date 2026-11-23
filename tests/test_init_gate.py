"""The initialisation gate: a deck's full run waits for a person to accept its initial state."""
import shutil
import sqlite3
from dataclasses import replace

import pytest

from opmagent.core import db, studies
from opmagent.core.errors import ToolError
from opmagent.deck import init_review as ir

pytestmark = pytest.mark.skipif(not shutil.which("flow"), reason="the initialisation view needs the OPM Flow binary")


def _hello_deck(cfg):
    from tests.test_init_view import hello_deck
    return hello_deck(cfg)          # the four hello models approved; the HELLO deck built and initialised


def test_full_run_waits_for_acceptance(cfg):
    from opmagent.servers.sim_server import SimService, wrap
    d = _hello_deck(cfg)
    con = db.connect(cfg.database)
    assert d["init_review"] == "waiting" and "accept-init" in d["spoken_summary"]
    assert ir.pending(con)[0]["id"] == d["deck_id"]
    r = wrap(SimService(cfg), "run_simulation")(deck_id=d["deck_id"])
    assert not r["ok"] and r["code"] == ir.NOT_ACCEPTED and "accept-init" in r["message"]

    a = ir.accept(cfg, con, d["deck_id"], "tester")
    assert a["review"]["decision"] == "accepted" and a["review"]["init_view_sha256"]
    assert ir.pending(con) == [] and ir.state(con, d["deck_id"]) == "accepted"
    with pytest.raises(ToolError, match="already accepted"):
        ir.accept(cfg, con, d["deck_id"], "someone else")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM init_reviews")
    r = wrap(SimService(cfg), "run_simulation")(deck_id=d["deck_id"])
    assert r["ok"], r


def test_rejection_goes_to_the_model(cfg):
    from opmagent.servers.data_server import DataService, wrap
    d = _hello_deck(cfg)
    con = db.connect(cfg.database)
    with pytest.raises(ToolError, match="needs a comment"):
        ir.reject(cfg, con, d["deck_id"], "tester", " ")
    r = wrap(DataService("reviewer", cfg), "reject_init_view")(deck_id=d["deck_id"], comment="OWC too deep", to="res")
    assert r["ok"] and r["feedback"]["model_kind"] == "res" and "OWC too deep" in r["feedback"]["comment"]
    with pytest.raises(ToolError, match="was rejected by"):
        ir.require(con, d["deck_id"])
    v = wrap(DataService("run", cfg), "init_view")(deck_id=d["deck_id"])
    assert v["init_review"]["decision"] == "rejected"
    # agents can't accept: the tools are on the reviewer profile only
    from opmagent.servers.data_server import TOOLS
    assert all("accept_init_view" not in TOOLS[p] for p in TOOLS if p != "reviewer")


def test_auto_accept_only_when_every_check_passes(cfg):
    from opmagent.deck.build import build_deck
    _hello_deck(cfg)
    con = db.connect(cfg.database)
    auto = replace(cfg, auto_approve=True)
    ids = {k: con.execute(f"SELECT id FROM {k}_model_versions WHERE status = 'approved'").fetchone()[0]
           for k in ("fluid", "geo", "res", "well")}
    d = build_deck({"name": "HELLO2", **ids, "years": 1}, auto, origin="demo")
    assert d["init_review"] == "accepted" and "accepted automatically" in d["spoken_summary"]
    assert ir.latest(con, d["deck_id"])["reviewer"] == ir.AUTO_REVIEWER
    assert ir.auto_accept(auto, con, 0, {"checks": [{"check": "water below OWC", "status": "warn"}]}) == \
        {"auto": False, "reason": "water below OWC"}
    assert ir.auto_accept(cfg, con, 0, {"checks": []}) is None          # auto-approve off


def test_study_deck_task_needs_acceptance(cfg):
    d = _hello_deck(cfg)
    con = db.connect(cfg.database)
    studies.init_study(con, cfg.studies_dir, "GATE", "init gate", actor="t")
    for t in ("data", "fluid", "geo", "res", "well"):
        con.execute("UPDATE study_tasks SET status = 'done' WHERE task = ?", (t,))
    con.execute("UPDATE study_tasks SET status = 'in_progress' WHERE task = 'deck'")
    with pytest.raises(ToolError, match="hasn't been accepted"):
        studies.complete_task(con, "GATE", "deck", {"deck_id": d["deck_id"]})
    ir.accept(cfg, con, d["deck_id"], "tester")
    r = studies.complete_task(con, "GATE", "deck", {"deck_id": d["deck_id"]})
    assert r["unlocked"] == ["run"]


def test_cli_needs_a_person(cfg, monkeypatch, capsys, tmp_path):
    from opmagent.cli import main
    d = _hello_deck(cfg)
    toml = tmp_path / "opmagent.toml"
    toml.write_text(f'[paths]\ndatabase = "{cfg.database}"\nfile_store = "{cfg.file_store}"\n')
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="interactive terminal"):
        main(["--config", str(toml), "review", "accept-init", str(d["deck_id"])])
    out = capsys.readouterr().out
    assert "oil in place" in out and "results open --deck" in out
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "accept")
    main(["--config", str(toml), "review", "accept-init", str(d["deck_id"])])
    assert ir.state(db.connect(cfg.database), d["deck_id"]) == "accepted"
