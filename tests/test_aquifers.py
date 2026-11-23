"""Analytical aquifers in the res model: built from the spec, validated, rendered (AQUDIMS, AQUFETP, AQUCT, AQUANCON)
and run in OPM Flow with influx."""
import shutil
from dataclasses import replace

import pytest

from opmagent.core.errors import ToolError
from opmagent.res.pipeline import aquifer_rows

CONTACTS = [{"eqlnum": 1, "datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100}]
FET = {"type": "fetkovich", "ct": 1e-4, "water_volume": 5e8, "productivity_index": 50,
       "connections": [{"face": "I-", "layers": "2-3"}]}
CT = {"type": "carter_tracy", "ct": 1e-4, "permeability": 200, "porosity": 0.25, "inner_radius": 2000,
      "thickness": 30, "connections": [{"face": "J+", "i": "3-8"}], "source": "analogue"}


def test_rows_and_defaults():
    aqs, conns, notes = aquifer_rows([FET, CT], (10, 10, 3), None, CONTACTS)
    assert [a["aq_type"] for a in aqs] == ["FETKOVICH", "CARTER_TRACY"]
    assert aqs[0]["datum_depth"] == 2100 and aqs[0]["init_pressure"] is None       # the OWC; equilibrated
    assert aqs[1]["angle_deg"] == 360 and aqs[1]["influence_table"] == 1
    assert conns[0] == {"aquifer_id": 1, "i1": 1, "i2": 1, "j1": 1, "j2": 10, "k1": 2, "k2": 3, "face": "I-",
                        "influx_mult": 1.0}
    assert (conns[1]["j1"], conns[1]["j2"], conns[1]["i1"], conns[1]["i2"], conns[1]["k1"], conns[1]["k2"]) == \
        (10, 10, 3, 8, 1, 3)                                                       # the J+ edge, rows 3-8
    assert "20 cell face(s)" in notes[0] and "OWC of EQLNUM 1" in notes[0] and "(analogue)" in notes[1]


@pytest.mark.parametrize("bad, msg", [
    ({**FET, "type": "numerical"}, "fetkovich or carter_tracy"),
    ({k: v for k, v in FET.items() if k != "productivity_index"}, "give productivity_index"),
    ({**FET, "connections": []}, "give its connections"),
    ({**FET, "connections": [{"face": "X+"}]}, "face must be"),
    ({**FET, "connections": [{"face": "I-", "layers": "2-4"}]}, "outside 1-3"),
    ({**FET, "connections": [{"face": "I-", "zone": "Lower"}]}, "needs a geo version built by the geo modeler"),
])
def test_rows_refuse(bad, msg):
    with pytest.raises(ToolError, match=msg):
        aquifer_rows([bad], (10, 10, 3), None, CONTACTS)


def test_zone_connection():
    gs = {"surfaces": ["Upper", "Lower", "Base"], "layers": [2, 1]}
    _, conns, _ = aquifer_rows([{**FET, "connections": [{"face": "K+", "zone": "Lower"}]}], (10, 10, 3), gs, CONTACTS)
    assert (conns[0]["k1"], conns[0]["k2"], conns[0]["i1"], conns[0]["i2"]) == (3, 3, 1, 10)


@pytest.mark.skipif(not shutil.which("flow"), reason="needs the OPM Flow binary")
def test_hello_deck_with_aquifers_runs_with_influx(cfg, tmp_path):
    from opmagent.deck.build import build_deck
    from opmagent.hello.models import DATUM_DEPTH, DATUM_P, OWC
    from opmagent.res.pipeline import build_res_model
    from opmagent.sim.runner import run_deck, summary_values
    from tests.test_init_view import hello_deck

    hello_deck(cfg)                                   # the four hello models, approved
    cfg = replace(cfg, auto_approve=True)
    from opmagent.core import db
    con = db.connect(cfg.database)
    geo_v = con.execute("SELECT id FROM geo_model_versions WHERE status = 'approved'").fetchone()[0]
    r = build_res_model(cfg, name="HELLO_RES_AQ", geo_version_id=geo_v, origin="demo", spec={
        "scal": {"swc": 0.2, "sorw": 0.2, "source": "test"}, "rock": {"p_ref": DATUM_P, "cr": 4.5e-5},
        "contacts": {"datum_depth": DATUM_DEPTH, "datum_pressure": DATUM_P, "owc_depth": OWC},
        "aquifers": [FET, CT]})
    assert r["ok"] and r["status"] == "approved", r
    assert "2 analytical aquifer(s)" in r["spoken_summary"]
    d = build_deck({"name": "HELLO_AQ", "fluid": "HELLO_FLUID", "geo": "HELLO_GEO", "res": r["version_id"],
                    "well": "HELLO_WELLS", "years": 1, "report": "quarterly"}, cfg)
    assert d["ok"], d.get("validation_log")
    text = open(d["path"]).read()
    assert "AQUDIMS\n 0 0 1 36 2 20 /" in text
    assert "AQUFETP\n 1 2100 1* 500000000 0.0001 50 1 /" in text
    assert "AQUANCON\n 1 1 1 1 10 2 3 'I-' 1* 1 /\n 2 3 8 10 10 1 3 'J+' 1* 1 /" in text
    assert "AAQR\n 1 2 /" in text
    assert d["init_view"]["in_place"]["FOIP"] > 0          # it initialises with the aquifers
    run = run_deck(d["path"], tmp_path / "run", cfg.flow_bin)
    assert run["ok"], run
    s = summary_values(run["smspec"], ["AAQT:1", "AAQT:2", "AAQP:1"])
    assert "AAQT:1" in s and "AAQT:2" in s and abs(s["AAQT:2"]["last"]) > 0
