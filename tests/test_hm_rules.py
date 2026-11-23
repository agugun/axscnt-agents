"""History matching rules found on Norne (phase 4): controls judged month by month, and parameters refused when the
deck's own passthrough text rewrites the keyword after the model's array. No Flow."""
import pandas as pd
import pytest

from opmagent.core.errors import ToolError
from opmagent.hm import misfit as mf
from opmagent.hm import params as pm
from opmagent.hm import series


def test_honoured_month_by_month():
    # a well under ORAT until March, then RESV: oil is given to Flow only in Jan-Feb
    hon = {"P1": {"controls": ["ORAT", "RESV"], "honoured": ["oil"],
                  "timeline": [("2020-01-01", "ORAT"), ("2020-03-01", "RESV"), ("2020-05-01", None)]}}
    months = [f"2020-{m:02d}-01" for m in range(1, 7)]
    df = pd.DataFrame([("P1", q, mo, 1.0, 30) for q in ("oil", "water") for mo in months], columns=series.COLUMNS)
    mask = mf.honoured_mask(df, hon)
    assert list(mask[:6]) == [True, True, False, False, False, False] and not mask[6:].any()
    obs = df.copy()
    sim = df.assign(value=2.0)
    r = mf.compute(obs, sim, mf.objective(), hon)
    oil = next(t for t in r["terms"] if t["quantity"] == "oil")
    assert oil["in_total"] and oil["n"] == 4 and oil["honoured_months"] == 2


def test_honoured_without_timeline_is_whole_history():
    df = pd.DataFrame([("P1", "oil", "2020-01-01", 1.0, 31)], columns=series.COLUMNS)
    assert mf.honoured_mask(df, {"P1": {"controls": ["ORAT"], "honoured": ["oil"]}}).all()


def _m(passthrough):
    """A deck_models()-like dict: one 3-layer geo with a fault, Corey res, the given passthrough."""
    geo = {"header": {"id": 1, "nx": 2, "ny": 1, "nz": 3, "spec_json": None},
           "tables": {"geo_properties": [{"keyword": k, "const_value": 1.0, "file_id": None}
                                         for k in ("PERMX", "PERMY", "PERMZ", "PORO")],
                      "faults": [{"fault_name": "F1"}], "fault_multipliers": []}}
    res = {"header": {"spec_json": {"res_spec": {"scal": {"nw": 2.0}}}}, "tables": {"swof": [], "aquifers": []}}
    return {"geo": geo, "res": res, "deck": {"spec_json": {"passthrough": passthrough}}}


def test_rewritten_keywords_are_refused():
    norne_like = {"GRID": [{"keyword": "PERMZ", "raw_text": "COPY\n 'PERMX' 'PERMZ' 1 46 1 112 1 22 /\n/\n"},
                           {"keyword": "PERMZ", "raw_text": "MULTIPLY\n 'PERMZ' 0.2 1 46 1 112 1 1 /\n/\n"}],
                  "EDIT": [{"keyword": "MULTFLT", "raw_text": "MULTFLT\n 'F1' 0.5 /\n/\n"}]}
    assert pm.rewritten({"passthrough": norne_like}, "PERMX") == []
    assert len(pm.rewritten({"passthrough": norne_like}, "PERMZ")) == 2
    ops = {"GRID": [{"keyword": "MULTIPLY", "raw_text": "MULTIPLY\n 'PORO' 0.9 /\n/\n"},
                    {"keyword": "COPY", "raw_text": "COPY\n 'PORO' 'NTG' /\n/\n"}]}
    assert pm.rewritten({"passthrough": ops}, "PORO") == ["GRID MULTIPLY PORO 0.9"]
    assert pm.rewritten({"passthrough": ops}, "NTG") == ["GRID COPY PORO NTG"]
    m = _m(norne_like)
    for kind, target, low, high, msg in (("permz_mult", {}, 0.5, 2, "sets PERMZ itself"),
                                         ("multflt", {"fault": "F1"}, 0, 1, "sets MULTFLT itself")):
        with pytest.raises(ToolError, match=msg):
            pm.check([{"name": "x", "kind": kind, "target": target, "low": low, "high": high}], m)
    ok = pm.check([{"name": "kx", "kind": "permx_mult", "target": {"layers": "1-2"}, "low": 0.5, "high": 2}], m)
    assert ok[0]["start"] == 1.0


def test_grid_output_dropped_and_disk_check(tmp_path, cfg):
    from opmagent.hm import jobs
    run = tmp_path / "run"
    run.mkdir()
    for name, size in (("C.UNRST", 1000), ("C.INIT", 10), ("C.EGRID", 10), ("C.SMSPEC", 5), ("C.UNSMRY", 5),
                       ("C.PRT", 5)):
        (run / name).write_bytes(b"x" * size)
    (run / "C.DATA").symlink_to(run / "C.PRT")
    assert jobs.drop_grid_output(run) == 1020
    assert sorted(p.name for p in run.iterdir()) == ["C.DATA", "C.PRT", "C.SMSPEC", "C.UNSMRY"]
    cfg.hm_min_free_disk_mb = 0
    assert jobs.disk_ok(cfg, tmp_path / "not" / "yet")
    cfg.hm_min_free_disk_mb = 10 ** 12
    assert not jobs.disk_ok(cfg, tmp_path)
