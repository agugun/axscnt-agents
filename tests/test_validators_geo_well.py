"""Geo and well validator rules on a small box model (the hello defaults)."""
from opmagent.core import models
from opmagent.core.db import connect

GEO_HDR = dict(unit_system="METRIC", grid_type="cartesian", nx=10, ny=10, nz=3, dx=100, dy=100, dz=10, tops=2000)


def corey_swof(swc=0.2, sorw=0.2, n=6):
    out = []
    for i in range(n):
        sw = swc + (1 - swc - sorw) * i / (n - 1)
        s = (sw - swc) / (1 - swc - sorw)
        out.append(dict(sw=round(sw, 6), krw=round(0.4 * s ** 2, 6), krow=round((1 - s) ** 2, 6), pcow=0))
    out.append(dict(sw=1.0, krw=1.0, krow=0.0, pcow=0))
    return out


def corey_sgof(swc=0.2, sgr=0.05, sorg=0.1, n=6):
    sgmax = 1 - swc - sorg
    out = [dict(sg=0.0, krg=0.0, krog=1.0, pcog=0)]
    for i in range(1, n):
        sg = sgr + (sgmax - sgr) * i / (n - 1)
        s = (sg - sgr) / (sgmax - sgr)
        out.append(dict(sg=round(sg, 6), krg=round(0.8 * s ** 2, 6), krog=round((1 - s) ** 2, 6), pcog=0))
    return out


def make_geo(con, **overrides):
    v = models.create_version(con, "geo", "BOX", {**GEO_HDR, **overrides})
    vid = v["version_id"]
    models.write_tables(con, "geo", vid, "geo_properties", [
        dict(keyword="PORO", const_value=0.2), dict(keyword="PERMX", const_value=100),
        dict(keyword="PERMY", const_value=100), dict(keyword="PERMZ", const_value=10)])
    models.write_tables(con, "geo", vid, "swof", corey_swof())
    models.write_tables(con, "geo", vid, "sgof", corey_sgof())
    models.write_tables(con, "geo", vid, "rock", [dict(p_ref=200, cr=4.5e-5)])
    models.write_tables(con, "geo", vid, "contacts", [dict(datum_depth=2000, datum_pressure=200, owc_depth=2100)])
    return vid


def make_well(con, geo_vid):
    v = models.create_version(con, "well", "WELLS", dict(unit_system="METRIC", geo_version_id=geo_vid, start_date="2026-01-01"))
    vid = v["version_id"]
    models.write_tables(con, "well", vid, "wells", [
        dict(well_name="PROD1", well_type="PRODUCER", preferred_phase="OIL", head_i=10, head_j=10, ref_depth=2000),
        dict(well_name="INJ1", well_type="INJECTOR", preferred_phase="WATER", head_i=1, head_j=1, ref_depth=2000)])
    models.write_tables(con, "well", vid, "well_completions", [
        dict(well_name="PROD1", event_date="2026-01-01", i=10, j=10, k1=1, k2=3, diameter=0.2),
        dict(well_name="INJ1", event_date="2026-01-01", i=1, j=1, k1=1, k2=3, diameter=0.2)])
    models.write_tables(con, "well", vid, "well_events", [
        dict(well_name="PROD1", event_date="2026-01-01", mode="PREDICTION", control="ORAT", oil_rate=500, bhp=100),
        dict(well_name="INJ1", event_date="2026-01-01", mode="PREDICTION", control="RATE", inj_rate=600, bhp=350)])
    return vid


def rules(con, kind, vid):
    r = models.run_validator(con, kind, vid)
    return r["passed"], {e["rule"] for e in r["errors"]}


def test_box_geo_and_wells_pass(cfg):
    con = connect(cfg.database)
    g = make_geo(con)
    assert rules(con, "geo", g) == (True, set())
    w = make_well(con, g)
    assert rules(con, "well", w) == (True, set())


def test_geo_failures(cfg, tmp_path):
    con = connect(cfg.database)
    g = make_geo(con)
    bad = tmp_path / "poro.inc"
    bad.write_text("PORO\n 299*0.2 1.4 /\n")  # 300 values, one > 1
    from opmagent.core.files import register_file
    fid = register_file(con, bad, "property")
    con.execute("UPDATE geo_properties SET const_value=NULL, file_id=? WHERE version_id=? AND keyword='PORO'", (fid, g))
    con.execute("UPDATE swof SET krow=0.5 WHERE version_id=? AND sw=1.0", (g,))
    ok, r = rules(con, "geo", g)
    assert not ok and {"range", "swof_end", "swof_krow"} <= r


def test_well_failures(cfg):
    con = connect(cfg.database)
    g = make_geo(con)
    w = make_well(con, g)
    con.execute("UPDATE well_events SET bhp=NULL WHERE version_id=? AND well_name='PROD1'", (w,))
    con.execute("UPDATE well_completions SET i=11 WHERE version_id=? AND well_name='PROD1'", (w,))
    con.execute("UPDATE well_events SET control='ORAT' WHERE version_id=? AND well_name='INJ1'", (w,))
    ok, r = rules(con, "well", w)
    assert not ok and {"bhp_limit", "perf_ijk", "control"} <= r


def test_extra_saturation_tables_are_a_warning(cfg):
    # Norne: SATNUM is 1 everywhere, table 2 is the imbibition table selected by IMBNUM (passthrough)
    con = connect(cfg.database)
    vid = make_geo(con)
    models.write_tables(con, "geo", vid, "swof", [dict(r, satnum=2) for r in corey_swof(swc=0.25)])
    models.write_tables(con, "geo", vid, "sgof", [dict(r, satnum=2) for r in corey_sgof(swc=0.25)])
    r = models.run_validator(con, "geo", vid)
    assert "regions" not in {e["rule"] for e in r["errors"]}
    assert any(w["rule"] == "regions" and "IMBNUM" in w["message"] for w in r["warnings"])
    # a SATNUM region with no table is still an error
    vid2 = make_geo(con)
    models.write_tables(con, "geo", vid2, "geo_properties", [dict(keyword="SATNUM", const_value=2)])
    errs = models.run_validator(con, "geo", vid2)["errors"]
    assert any("SWOF has no table for SATNUM [2]" in e["message"] for e in errs), errs
