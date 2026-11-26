"""QC notebooks: the standard template as the QC report, re-runs, investigations, and the table checks."""
import dataclasses
import json

import nbformat
import pytest

from opmagent.fluid.excel import read_lab_report
from opmagent.fluid.pipeline import build_fluid_model
from opmagent.notebooks import qc_lib
from opmagent.servers.data_server import DataService, wrap
from opmagent.servers.pvt_server import PVTService
from tests.conftest import fluid_rows
from tests.test_fluid_pipeline import lab_workbook, truth  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def pvt():
    return PVTService()


def call(svc, name, **kw):
    return wrap(svc, name)(**kw)


def test_notebook_qc_report_rerun_and_investigation(tmp_path, cfg, pvt, truth):  # noqa: F811
    fi, gaps = read_lab_report(lab_workbook(tmp_path / "lab.xlsx", truth))
    fl = DataService("fluid", cfg)
    r = build_fluid_model(fi, "FieldA_Oil", data=fl, pvt=pvt, gaps=gaps)
    assert r["ok"] and r["status"] == "qc_ready", r
    assert r["qc_report"].endswith("fluid_qc.html") and r["qc_overall"] == "pass", r
    folder = cfg.file_store / "qc" / "fluid_FieldA_Oil_v1"
    summary = json.loads((folder / "qc_summary.json").read_text())
    assert summary["tables_from_db"] and summary["counts"]["fail"] == 0
    assert {f["area"] for f in summary["findings"]} == {"lab data", "lab vs model", "tables"}
    html = (folder / "fluid_qc.html").read_text()
    assert "FieldA_Oil version 1" in html and "within tolerance" in html and "Undersaturated Bo" in html
    # the report the reviewer sees is the registered, hashed file
    qc = DataService("reviewer", cfg).get_qc_report(kind="fluid", version_id=r["version_id"])
    assert qc["qc"]["metrics"]["qc_overall"] == "pass" and len(qc["qc"]["metrics"]["qc_notebook_sha256"]) == 64

    # frozen while under review; after return_to_draft the QC can be re-run and resubmitted
    vid = r["version_id"]
    assert call(fl, "run_qc_notebook", version_id=vid)["code"] == "FORBIDDEN"
    assert call(fl, "return_to_draft", kind="fluid", version_id=vid)["ok"]
    rr = call(fl, "run_qc_notebook", version_id=vid)
    assert rr["ok"] and rr["overall"] == "pass", rr
    assert call(fl, "submit_for_review", kind="fluid", version_id=vid, qc_report_file_id=rr["qc_report_file_id"])["ok"]

    # investigation: starter notebook, the agent adds a cell, runs it; outputs and code are in the HTML
    inv = call(fl, "create_investigation_notebook", version_id=vid, purpose="C7+ MW sensitivity")
    assert inv["ok"] and inv["path"].endswith("01_C7_MW_sensitivity.ipynb")
    nb = nbformat.read(inv["path"], as_version=4)
    nb.cells[4].source = "from opmagent.pvt import engine\nprint('PSAT', round(engine.saturation_pressure(spec), 1))"
    nbformat.write(nb, inv["path"])
    out = call(fl, "run_investigation_notebook", path=inv["path"])
    assert out["ok"] and out["ran_clean"], out
    assert "PSAT" in open(out["html"]).read() and "saturation_pressure" in open(out["html"]).read()
    assert call(fl, "create_investigation_notebook", version_id=vid, purpose="second")["path"].split("/")[-1][:3] == "02_"
    # only notebooks under the QC folder can be run
    stray = tmp_path / "stray.ipynb"
    nbformat.write(nbformat.v4.new_notebook(), stray)
    assert call(fl, "run_investigation_notebook", path=str(stray))["code"] == "INVALID_INPUT"


def test_failing_notebook_keeps_the_draft(tmp_path, cfg, pvt, truth, monkeypatch):  # noqa: F811
    from opmagent.notebooks import runner
    bad = tmp_path / "bad.ipynb"
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1", metadata={"tags": ["parameters"]}),
                                         nbformat.v4.new_code_cell("raise RuntimeError('boom')")])
    nbformat.write(nb, bad)
    monkeypatch.setattr(runner, "FLUID_TEMPLATE", bad)
    fi, gaps = read_lab_report(lab_workbook(tmp_path / "lab.xlsx", truth))
    r = build_fluid_model(fi, "FieldB_Oil", data=DataService("fluid", cfg), pvt=pvt, gaps=gaps)
    assert not r["ok"] and r["status"] == "draft" and "boom" in r["error"]


def test_html_report_mode_still_works(tmp_path, cfg, pvt, truth):  # noqa: F811
    cfg = dataclasses.replace(cfg, qc_report="html")
    fi, gaps = read_lab_report(lab_workbook(tmp_path / "lab.xlsx", truth))
    r = build_fluid_model(fi, "FieldC_Oil", data=DataService("fluid", cfg), pvt=pvt, gaps=gaps, inline_js=False)
    assert r["ok"] and r["qc_report"].endswith("fluid_FieldC_Oil_v1.html") and "qc_notebook" not in r


def _data(tables):
    return {"tables": tables, "tables_from_db": True, "p_range": [20, 400]}


def test_table_checks_flag_what_opm_rejects():
    good = {k: [dict(r, pvtnum=1) for r in v] for k, v in fluid_rows().items()}
    assert all(f["status"] == "pass" for f in qc_lib.table_checks(_data(good))
               if f["check"] != "PVTO covers the maximum pressure")
    bad = json.loads(json.dumps(good))
    bad["pvto"][-1]["bo"] = 1.40             # undersaturated Bo rises with pressure
    bad["pvdg"][2]["bg"] = 0.009             # Bg not monotonic
    res = {f["check"]: f["status"] for f in qc_lib.table_checks(_data(bad))}
    assert res["Undersaturated Bo falls with pressure"] == "fail"
    assert res["PVDG Bg falls with pressure"] == "fail"
    top_only = json.loads(json.dumps(good))
    top_only["pvto"] = [r for r in top_only["pvto"] if not (r["rs"] == 100 and not r["is_saturated"])]
    res = {f["check"]: f["status"] for f in qc_lib.table_checks(_data(top_only))}
    assert res["Highest Rs block has an undersaturated branch"] == "fail"


def _kernel_children() -> list[int]:
    """ipykernel processes started by this test process (its children)."""
    import os
    from pathlib import Path
    me, out = os.getpid(), []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            stat = (p / "stat").read_text()
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if int(stat.rsplit(")", 1)[1].split()[1]) == me and b"ipykernel_launcher" in cmd:
            out.append(int(p.name))
    return out


@pytest.mark.parametrize("fail", [False, True])
def test_no_kernel_left_running(tmp_path, fail):
    """Each notebook run shuts its kernel down, passed or failed (a leak ran long sessions out of memory)."""
    from opmagent.notebooks import runner
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell("x = 1" if not fail else "raise ValueError('boom')")]
    src = tmp_path / "t.ipynb"
    nbformat.write(nb, src)
    before = set(_kernel_children())
    for _ in range(3):
        r = runner.execute(src, tmp_path / "out.ipynb")
        assert r["ok"] is (not fail)
    assert set(_kernel_children()) - before == set()


def test_notebook_kernel_talks_over_ipc(tmp_path, capfd):
    """QC kernels use IPC sockets in a private folder (removed after), not TCP: no ports, and no 'Kernel is running
    over TCP without encryption' warning on every QC run."""
    import os
    import tempfile

    import nbformat

    from opmagent.notebooks import runner
    if os.name != "posix":
        pytest.skip("IPC transport is POSIX only")
    km = runner._kernel_manager()
    assert km.transport == "ipc" and oct(km._opmagent_socket_dir.stat().st_mode & 0o777) == "0o700"
    km._opmagent_socket_dir.rmdir()
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("print('IPC', 6 * 7)")])
    src = tmp_path / "t.ipynb"
    nbformat.write(nb, src)
    before = set(os.listdir(tempfile.gettempdir()))
    r = runner.execute(src, tmp_path / "out" / "t.ipynb")
    assert r["ok"], r
    assert "IPC 42" in (tmp_path / "out" / "t.ipynb").read_text()
    assert "without encryption" not in capfd.readouterr().err
    assert not {d for d in set(os.listdir(tempfile.gettempdir())) - before if d.startswith("opmagent-kernel-")}
