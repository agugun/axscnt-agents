"""Static view of a geo model: a static deck made into EGRID + INIT by an OPM Flow dry run, for ResInsight."""
import shutil

import pytest

from opmagent.core import db
from opmagent.core.errors import ToolError
from opmagent.geo.static_view import build_static_view, latest_res_on, static_case
from opmagent.hello import models as hm

pytestmark = pytest.mark.skipif(not shutil.which("flow"), reason="needs the OPM Flow binary")


def init_arrays(path):
    from opm.io.ecl import EclFile
    return {a[0] for a in EclFile(path).arrays}


def test_hello_geo_with_its_res_regions(cfg):
    con = db.connect(cfg.database)
    g = hm.build_geo(cfg)
    assert static_case(cfg, con, g["version_id"]) is None
    v = build_static_view(cfg, con, g["version_id"])                     # the geo model alone, a draft under review
    assert v["ok"] and v["active_cells"] == 300 and v["egrid"].endswith(".EGRID"), v
    assert {"PORV", "DEPTH", "PORO", "PERMX", "NTG", "TRANX"} <= init_arrays(v["init"])
    text = open(v["deck"]).read()
    assert "not for simulation" in text and "Not a simulation deck" in text
    assert con.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 0  # never registered as a deck
    r = hm.build_res(cfg, g["version_id"])
    assert latest_res_on(con, g["version_id"]) == r["version_id"]
    v2 = build_static_view(cfg, con, g["version_id"], res_version_id=r["version_id"])
    assert "res HELLO_RES v1" in open(v2["deck"]).read()
    assert static_case(cfg, con, g["version_id"]) is not None


def test_res_on_another_geo_is_refused(cfg):
    con = db.connect(cfg.database)
    g1, g2 = hm.build_geo(cfg), hm.build_geo(cfg)
    r = hm.build_res(cfg, g1["version_id"])
    with pytest.raises(ToolError, match="defined on geo version"):
        build_static_view(cfg, con, g2["version_id"], res_version_id=r["version_id"])


def test_geo_build_makes_it_and_results_open_finds_it(cfg, tmp_path):
    pytest.importorskip("lasio")
    from opmagent.geo.pipeline import build_geo_model
    from opmagent.viewer import find_case
    from tests.test_geo_modeler import SPEC, write_inputs
    tops, logs = write_inputs(tmp_path)
    g = build_geo_model(cfg, tops=tops, logs=logs, spec=SPEC, name="FieldG_Geo")
    assert g["ok"] and g["static_view"].endswith(".EGRID"), g
    assert "results open --geo" in g["spoken_summary"]
    egrid, where = find_case(cfg, geo=g["version_id"])
    assert str(egrid) == g["static_view"] and "static view" in where


def test_mcp_tool_on_the_geo_profile(cfg):
    from opmagent.servers.data_server import TOOLS, DataService, wrap
    assert "static_view" in TOOLS["geo"] and "static_view" in TOOLS["view"] and "static_view" not in TOOLS["fluid"]
    g = hm.build_geo(cfg)
    r = wrap(DataService("geo", cfg), "static_view")(version_id=g["version_id"])
    assert r["ok"] and r["active_cells"] == 300, r


def test_res_built_later_brings_its_regions(cfg):
    """A geo view is made with the geo build, before any res model; building the res model, or opening the view
    after one was built, makes it again with the res model's region arrays."""
    from opmagent.geo.static_view import current_case, view_res
    con = db.connect(cfg.database)
    g = hm.build_geo(cfg)
    build_static_view(cfg, con, g["version_id"])
    assert view_res(cfg, con, g["version_id"]) is None
    r = hm.build_res(cfg, g["version_id"])                      # the res pipeline remakes the geo's view
    assert r["static_view"].endswith(".EGRID") and view_res(cfg, con, g["version_id"]) == r["version_id"]
    build_static_view(cfg, con, g["version_id"])                # someone remade it without the res model
    current_case(cfg, con, g["version_id"])                     # opening it brings the res model back in
    assert view_res(cfg, con, g["version_id"]) == r["version_id"]
