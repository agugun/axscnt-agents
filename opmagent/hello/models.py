"""Hello demo models: a 10x10x3 box, the default oil, Corey SCAL and contacts, one producer and one water injector.

All four (fluid, geo, res, well) are created as demo versions (origin 'demo') through the normal data server profiles,
validated, given a QC report and submitted for review. A human still approves them.
"""
from __future__ import annotations

from ..fluid.excel import read_lab_report, write_template
from ..fluid.pipeline import build_fluid_model
from ..reports.simple_report import geo_report, well_report
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from ..servers.pvt_server import PVTService

FLUID, GEO, RES, WELLS = "HELLO_FLUID", "HELLO_GEO", "HELLO_RES", "HELLO_WELLS"
GRID = dict(nx=10, ny=10, nz=3, dx=100.0, dy=100.0, dz=10.0, tops=2000.0)
DATUM_DEPTH, DATUM_P, OWC = 2000.0, 200.0, 2100.0
START = "2026-01-01"


def _ok(r):
    if not r.get("ok"):
        raise RuntimeError(r.get("message") or r.get("spoken_summary") or str(r))
    return r


def corey_swof(swc=0.2, sorw=0.2, nw=2.0, no=2.0, krw_max=0.4, n=8):
    rows = []
    for i in range(n):
        sw = swc + (1 - swc - sorw) * i / (n - 1)
        s = min(1.0, max(0.0, (sw - swc) / (1 - swc - sorw)))   # rounding can pass 1: (1 - s) ** 1.5 complex
        rows.append(dict(sw=round(sw, 6), krw=round(krw_max * s ** nw, 6), krow=round((1 - s) ** no, 6), pcow=0.0))
    rows.append(dict(sw=1.0, krw=1.0, krow=0.0, pcow=0.0))
    return rows


def corey_sgof(swc=0.2, sgc=0.05, sorg=0.1, ng=2.0, no=2.0, krg_max=0.8, n=8):
    sgmax = 1 - swc - sorg
    rows = [dict(sg=0.0, krg=0.0, krog=1.0, pcog=0.0)]
    for i in range(n):
        sg = sgc + (sgmax - sgc) * i / (n - 1)
        s = min(1.0, max(0.0, (sg - sgc) / (sgmax - sgc)))
        rows.append(dict(sg=round(sg, 6), krg=round(krg_max * s ** ng, 6), krog=round((1 - s) ** no, 6), pcog=0.0))
    rows.append(dict(sg=round(1 - swc, 6), krg=1.0, krog=0.0, pcog=0.0))
    return rows


def build_fluid(cfg, pvt: PVTService | None = None, origin: str = "demo", name: str = FLUID) -> dict:
    path = cfg.inbox / "HELLO_lab_report.xlsx"
    write_template(path, example=True)
    fi, gaps = read_lab_report(path)
    r = build_fluid_model(fi, name, data=DataService("fluid", cfg), pvt=pvt or PVTService(cfg), origin=origin,
                          user_description="hello demo oil", gaps=gaps)
    return _ok(r)


def build_geo(cfg, origin: str = "demo", name: str = GEO) -> dict:
    svc = DataService("geo", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    v = D("create_version", kind="geo", model_name=name, origin=origin,
          header={"unit_system": "METRIC", "grid_type": "cartesian", **GRID, "user_description": "hello demo box",
                  "assumptions_json": ["uniform properties"]})
    vid = v["version_id"]
    D("write_tables", kind="geo", version_id=vid, table="geo_properties", rows=[
        dict(keyword="PORO", const_value=0.20), dict(keyword="PERMX", const_value=100.0),
        dict(keyword="PERMY", const_value=100.0), dict(keyword="PERMZ", const_value=10.0), dict(keyword="NTG", const_value=1.0)])
    val = D("run_validator", kind="geo", version_id=vid)
    m = svc.get_model(kind="geo", version_id=vid)
    pv = GRID["nx"] * GRID["ny"] * GRID["nz"] * GRID["dx"] * GRID["dy"] * GRID["dz"] * 0.20
    metrics = {"grid": "10 x 10 x 3 cartesian, 100 x 100 x 10 m", "active cells": 300,
               "pore volume (rm3)": f"{pv:,.0f}"}
    rep = geo_report(cfg.file_store / "qc" / f"geo_{name}_v{v['version']}.html", model=m["header"], metrics=metrics,
                     assumptions=m["header"]["assumptions_json"], warnings=val["warnings"], swof=[], sgof=[])
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    D("submit_for_review", kind="geo", version_id=vid, qc_report_file_id=rid, metrics=metrics)
    return {"version_id": vid, "version": v["version"], "qc_report": str(rep)}


def build_res(cfg, geo_version_id: int, origin: str = "demo", name: str = RES) -> dict:
    """Corey SCAL (Swc 0.2, Sorw 0.2, Sorg 0.1), rock and contacts (OWC below the grid) on the hello box."""
    from ..res.pipeline import build_res_model
    return build_res_model(cfg, name=name, geo_version_id=geo_version_id, origin=origin,
                           description="hello demo SCAL, rock and contacts",
                           spec={"scal": {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "sgc": 0.05, "nw": 2.0, "now": 2.0,
                                          "ng": 2.0, "nog": 2.0, "krw_max": 0.4, "krg_max": 0.8,
                                          "source": "hello demo"},
                                 "rock": {"p_ref": DATUM_P, "cr": 4.5e-5},
                                 "contacts": {"datum_depth": DATUM_DEPTH, "datum_pressure": DATUM_P, "owc_depth": OWC}})


def build_wells(cfg, geo_version_id: int) -> dict:
    svc = DataService("well", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    v = D("create_version", kind="well", model_name=WELLS, origin="demo",
          header={"unit_system": "METRIC", "geo_version_id": geo_version_id, "start_date": START,
                  "user_description": "hello demo wells",
                  "assumptions_json": ["IJK completions on the hello grid", "prediction only, no history"]})
    vid = v["version_id"]
    D("write_tables", kind="well", version_id=vid, table="wells", rows=[
        dict(well_name="PROD1", well_type="PRODUCER", preferred_phase="OIL", head_i=10, head_j=10, ref_depth=DATUM_DEPTH),
        dict(well_name="INJ1", well_type="INJECTOR", preferred_phase="WATER", head_i=1, head_j=1, ref_depth=DATUM_DEPTH)])
    D("write_tables", kind="well", version_id=vid, table="well_completions", rows=[
        dict(well_name="PROD1", event_date=START, i=10, j=10, k1=1, k2=3, diameter=0.2),
        dict(well_name="INJ1", event_date=START, i=1, j=1, k1=1, k2=3, diameter=0.2)])
    D("write_tables", kind="well", version_id=vid, table="well_events", rows=[
        dict(well_name="PROD1", event_date=START, mode="PREDICTION", control="ORAT", oil_rate=500.0, bhp=100.0),
        dict(well_name="INJ1", event_date=START, mode="PREDICTION", control="RATE", inj_rate=600.0, bhp=350.0)])
    val = D("run_validator", kind="well", version_id=vid)
    m = svc.get_model(kind="well", version_id=vid)
    t = m["tables"]
    rep = well_report(cfg.file_store / "qc" / f"well_{WELLS}_v{v['version']}.html", model=m["header"],
                      wells=t["wells"], completions=t["well_completions"], events=t["well_events"],
                      assumptions=m["header"]["assumptions_json"], warnings=val["warnings"])
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    D("submit_for_review", kind="well", version_id=vid, qc_report_file_id=rid, metrics={"wells": 2})
    return {"version_id": vid, "version": v["version"], "qc_report": str(rep)}
