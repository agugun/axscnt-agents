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


def aquifer_deck(cfg):
    """The hello models, then a res version with one aquifer of each type and its deck (validated)."""
    from opmagent.core import db
    from opmagent.deck.build import build_deck
    from opmagent.hello.models import DATUM_DEPTH, DATUM_P, OWC
    from opmagent.res.pipeline import build_res_model
    from tests.test_init_view import hello_deck

    hello_deck(cfg)                                   # the four hello models, approved
    cfg = replace(cfg, auto_approve=True)
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
    return d, r["version_id"]


@pytest.mark.skipif(not shutil.which("flow"), reason="needs the OPM Flow binary")
def test_hello_deck_with_aquifers_runs_with_influx(cfg, tmp_path):
    from opmagent.sim.runner import run_deck, summary_values
    d, _ = aquifer_deck(cfg)
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


# ---------------------------------------------------------------- import: the deck's aquifers into the res model
class _Kw:
    def __init__(self, name, text=""):
        self.name, self._t = name, text

    def __str__(self):
        return f"{self.name}\n{self._t}"


@pytest.mark.parametrize("deck, why", [
    ([_Kw("AQUFET", " 2050 1* 5E8 1E-4 50 1 1 1 1 1 1 3 'I-' /")], ""),          # old form: no AQUFETP / AQUCT
    ([_Kw("AQUFETP", " 1 2050 1* 5E8 1E-4 50 1 /\n/"), _Kw("AQUTAB", " 0.1 0.2 /\n/"),
      _Kw("AQUANCON", " 1 1 1 1 10 1 3 'I-' /\n/")], "AQUTAB"),
    ([_Kw("AQUCT", " 1 2050 1* 200 0.25 1E-4 2000 30 360 1 2 /\n/"), _Kw("AQUANCON", " 1 1 1 1 1 1 1 'I-' /\n/")],
     "influence table 2"),
    ([_Kw("AQUFETP", " 1 2050 1* 5E8 1E-4 50 1 /\n/")], "without AQUANCON"),
    ([_Kw("AQUFETP", " 1 2050 1* 5E8 1E-4 50 1 /\n/"), _Kw("AQUANCON", " 1 1 1 1 10 1 3 'I-' 1* 1.0 'YES' /\n/")],
     "internal cell faces"),
    ([_Kw("AQUFETP", " 1 2050 1* 5E8 1E-4 50 1 30000 /\n/"), _Kw("AQUANCON", " 1 1 1 1 10 1 3 'I-' /\n/")],
     "salinity"),
])
def test_aquifers_kept_as_passthrough_when_they_dont_fit(deck, why):
    from opmagent.deck.importer import aquifer_mappable
    ok, reason = aquifer_mappable(deck)
    assert not ok and why in reason


def test_aquifer_tables_from_keywords():
    from opmagent.deck.importer import aquifer_mappable, aquifer_tables
    deck = [_Kw("AQUCT", " 2 2050 210 200 0.25 1E-4 2000 30 1* 1 1 /\n/"),
            _Kw("AQUFETP", " 1 2100 1* 5E8 1E-4 50 1 /\n/"),
            _Kw("AQUANCON", " 1 1 1 1 10 2 3 'X-' /\n 2 3 8 10 10 1 3 'J+' 1* 0.5 /\n/")]
    assert aquifer_mappable(deck) == (True, "")
    aqs, conns = aquifer_tables(deck)
    assert [a["aquifer_id"] for a in aqs] == [1, 2] and aqs[1]["init_pressure"] == 210 and aqs[1]["angle_deg"] == 360
    assert aqs[0]["init_pressure"] is None and aqs[0]["water_volume"] == 5e8
    assert conns[0]["face"] == "I-" and conns[0]["influx_mult"] == 1.0 and conns[1]["influx_mult"] == 0.5


@pytest.mark.skipif(not shutil.which("flow"), reason="needs the OPM Flow binary")
def test_import_round_trip_keeps_the_aquifers(cfg):
    pytest.importorskip("opm.simulators")
    from opmagent.core import db
    from tests.test_importer import roundtrip
    d, src_vid = aquifer_deck(cfg)
    r, v = roundtrip(cfg, d["path"], "AQIMP")
    con = db.connect(cfg.database)
    vid = con.execute("SELECT v.id FROM res_model_versions v JOIN res_models m ON m.id = v.res_model_id "
                      "WHERE m.name = 'AQIMP_RES'").fetchone()[0]
    cols = "aquifer_id, aq_type, datum_depth, init_pressure, total_compressibility, water_volume, " \
           "productivity_index, permeability, porosity, inner_radius, thickness, angle_deg"
    got = con.execute(f"SELECT {cols} FROM aquifers WHERE version_id = ? ORDER BY aquifer_id", (vid,)).fetchall()
    want = con.execute(f"SELECT {cols} FROM aquifers WHERE version_id = ? ORDER BY aquifer_id", (src_vid,)).fetchall()
    assert [tuple(x) for x in got] == [tuple(x) for x in want]
    assert con.execute("SELECT COUNT(*) FROM aquifer_connections WHERE version_id = ?", (vid,)).fetchone()[0] == 2
    import json
    (spec,) = (cfg.file_store / "imports" / "AQIMP").glob("import-*/deck_spec.json")
    kept = [x["keyword"] for sec in json.loads(spec.read_text())["passthrough"].values() for x in sec]
    assert not [k for k in kept if k.startswith("AQU")], kept          # mapped, not carried through
    assert v["ok"]
