"""A whole study, each step through the profile of the agent that does it: data manager -> fluid, geo, res, well
modelers (auto-approved) -> deck builder -> run agent -> results viewer."""
from dataclasses import replace

import pytest

pytest.importorskip("opm.simulators")
pytest.importorskip("lasio")
from opmagent.cli import _study_build  # noqa: E402
from opmagent.core import db, studies  # noqa: E402
from opmagent.servers.data_server import DataService, wrap  # noqa: E402
from opmagent.servers.sim_server import SimService  # noqa: E402
from opmagent.servers.sim_server import wrap as swrap  # noqa: E402
from opmagent.viewer import open_in_resinsight  # noqa: E402
from tests.study_data import write_raw  # noqa: E402


def test_study_end_to_end(cfg, tmp_path, monkeypatch):
    fake = tmp_path / "resinsight"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    cfg = replace(cfg, auto_approve=True, resinsight_bin=str(fake))
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    con = db.connect(cfg.database)

    st = call("deck", "init_study", name="FieldS", requirement="Waterflood, 2 years", years=2, report="quarterly",
              start_date="2026-01-01")
    folder = st["study"]["folder"]
    write_raw(tmp_path / "raw")
    import shutil
    shutil.copytree(tmp_path / "raw", f"{folder}/inbox", dirs_exist_ok=True)

    # data manager: inbox -> database
    call("data", "start_task", name="FieldS")
    m = call("data", "load_study_data", name="FieldS")
    assert m["all_ready"], m["issues"]
    assert call("data", "complete_task", name="FieldS", result={})["unlocked"] == ["fluid", "geo", "well"]
    assert call("geo", "study_data", name="FieldS")["datasets"]["logs"]["wells"] == ["A", "B", "C", "D"]

    # modelers: geo first so the res and well models know the grid
    unlocked = {}
    for kind in ("geo", "res", "fluid", "well"):
        call(kind, "start_task", name="FieldS")
        r = _study_build(cfg, con, kind, "FieldS")
        assert r["ok"], r
        assert r["status"] == "approved" and "approved automatically" in r["spoken_summary"], r["spoken_summary"]
        vid = r["version_id"]
        src = {row[0] for row in con.execute(f"SELECT f.kind FROM {kind}_version_sources s JOIN files f "
                                              f"ON f.id = s.file_id WHERE s.version_id = ?", (vid,))}
        assert src, f"{kind} version has no source files"
        done = call(kind, "complete_task", name="FieldS", result={"version_id": vid})
        assert done["ok"], done
        unlocked[kind] = done["unlocked"]
    assert unlocked == {"geo": ["res"], "res": [], "fluid": [], "well": ["deck"]}   # res waits for geo's grid
    res = con.execute("SELECT r.geo_version_id, g.id FROM res_model_versions r, geo_model_versions g").fetchone()
    assert res[0] == res[1]
    assert {r_[0] for r_ in con.execute("SELECT reviewer FROM approvals")} == {"auto (approval disabled)"}

    # deck builder
    call("deck", "start_task", name="FieldS")
    d = _study_build(cfg, con, "deck", "FieldS")
    assert d["ok"], d.get("validation_log")
    assert call("deck", "complete_task", name="FieldS", result={"deck_id": d["deck_id"]})["unlocked"] == ["run"]

    # run agent
    call("run", "start_task", name="FieldS")
    sim = SimService(cfg)
    rid = swrap(sim, "run_simulation")(deck_id=d["deck_id"])["run_id"]
    assert swrap(sim, "wait")(run_id=rid, timeout_s=300)["ok"]
    assert call("run", "complete_task", name="FieldS", result={"run_id": rid})["unlocked"] == ["view"]

    # results viewer
    call("view", "start_task", name="FieldS")
    monkeypatch.setenv("DISPLAY", ":0")
    v = open_in_resinsight(cfg, study="FieldS", dry_run=True)
    assert v["ok"] and v["command"][1:2] == ["--case"] and v["case"].endswith(".EGRID") and v["summary"]
    end = call("view", "complete_task", name="FieldS", result={"opened": v["case"]})
    assert end["study"]["status"] == "complete" and end["spoken_summary"].startswith("view done")
    assert studies.status(con, "FieldS")["spoken_summary"] == "Study FieldS is complete."
