"""opmagent backup and clean: a consistent database copy; clean lists first, removes only what is safe to remove."""
import json
import sqlite3
import tarfile

import pytest

from opmagent import maintenance as mt
from opmagent.cli import main
from opmagent.core import config as cfgmod


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    main(["setup", str(w)])
    cfg = cfgmod.load(str(w / "opmagent.toml"))
    fs = cfg.file_store
    # a failed run and a done run (raw rows: no deck needed for this test)
    con = sqlite3.connect(cfg.database)
    con.execute("PRAGMA foreign_keys = OFF")
    for rid, status in ((1, "failed"), (2, "done")):
        out = fs / "runs" / f"run{rid}_X"
        out.mkdir(parents=True)
        (out / "X.UNRST").write_bytes(b"0" * 1000)
        con.execute("INSERT INTO runs(id, deck_id, status, out_dir) VALUES (?, 99, ?, ?)", (rid, status, str(out)))
    con.commit()
    con.close()
    # imports of one deck: 001 (nothing approved), 002 (its fluid approved), 003 (newest)
    for n, fluid in ((1, 11), (2, 12), (3, 13)):
        d = fs / "imports" / "D" / f"import-{n:03d}"
        d.mkdir(parents=True)
        (d / "import.json").write_text(json.dumps({"versions": {"fluid": fluid, "geo": 20 + n, "res": 30 + n,
                                                                "well": 40 + n}}))
    (fs / "init_views" / "D_deck1").mkdir(parents=True)
    (fs / "qc" / "geo_D_GEO_v1" / "static").mkdir(parents=True)
    (fs / "qc" / "geo_D_GEO_v1" / "report.html").write_text("kept")
    return w, cfg


def test_clean_lists_then_removes_only_what_is_safe(ws, monkeypatch, capsys):
    w, cfg = ws
    fs = cfg.file_store
    monkeypatch.setattr(mt, "_approved", lambda con: {"fluid": {12}, "geo": set(), "res": set(), "well": set()})
    paths = {(i["category"], i["path"].name) for i in mt.plan(cfg)}
    assert paths == {("runs", "run1_X"), ("imports", "import-001"), ("views", "D_deck1"), ("views", "static")}

    main(["--config", str(w / "opmagent.toml"), "clean"])            # a dry run
    assert "Nothing removed" in capsys.readouterr().out
    assert (fs / "runs" / "run1_X").is_dir() and (fs / "imports" / "D" / "import-001").is_dir()

    main(["--config", str(w / "opmagent.toml"), "clean", "--runs", "--yes"])
    assert not (fs / "runs" / "run1_X").exists() and (fs / "runs" / "run2_X").is_dir()
    assert (fs / "imports" / "D" / "import-001").is_dir()          # not asked for
    con = sqlite3.connect(cfg.database)
    assert "opmagent clean" in con.execute("SELECT note FROM runs WHERE id=1").fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2   # the rows stay

    main(["--config", str(w / "opmagent.toml"), "clean", "--yes"])
    assert not (fs / "imports" / "D" / "import-001").exists()
    assert (fs / "imports" / "D" / "import-002").is_dir() and (fs / "imports" / "D" / "import-003").is_dir()
    assert not (fs / "init_views" / "D_deck1").exists() and not (fs / "qc" / "geo_D_GEO_v1" / "static").exists()
    assert (fs / "qc" / "geo_D_GEO_v1" / "report.html").is_file()
    assert mt.plan(cfg) == []


def test_clean_never_leaves_the_file_store(ws, tmp_path):
    w, cfg = ws
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    con = sqlite3.connect(cfg.database)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("INSERT INTO runs(id, deck_id, status, out_dir) VALUES (3, 99, 'failed', ?)", (str(outside),))
    con.commit()
    assert all(i["path"] != outside for i in mt.plan(cfg))
    mt.clean(cfg, [{"category": "runs", "path": outside, "run_id": 3, "bytes": 0}])
    assert outside.is_dir()


def test_backup(ws, tmp_path):
    w, cfg = ws
    out = tmp_path / "bk"
    main(["--config", str(w / "opmagent.toml"), "backup", "--out", str(out)])
    (archive,) = out.glob("ws-*.tar.gz")
    with tarfile.open(archive) as t:
        names = t.getnames()
        assert {"opm.db", "opmagent.toml", "manifest.json"} <= set(names)
        assert not any(n.startswith("files") for n in names)
        m = json.load(t.extractfile("manifest.json"))
        t.extract("opm.db", tmp_path / "x")
    assert m["rows"]["runs"] == 2 and m["with_files"] is False and m["database"] == str(cfg.database)
    copy = sqlite3.connect(tmp_path / "x" / "opm.db")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2

    r = mt.backup(cfg, config_path=w / "opmagent.toml", out=tmp_path / "bk2", files=True)
    with tarfile.open(r["archive"]) as t:
        names = t.getnames()
    assert "files/runs/run1_X/X.UNRST" in names and "files/qc/geo_D_GEO_v1/report.html" in names
    assert not any(n.startswith(("files/init_views/", "files/qc/geo_D_GEO_v1/static")) for n in names)
    assert "files/init_views" not in names
