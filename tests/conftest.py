import os
from pathlib import Path

import pytest

from opmagent.core import config as cfgmod
from opmagent.core import db
from opmagent.core.config import Config


@pytest.fixture(autouse=True)
def _no_default_workspace(monkeypatch):
    """Tests never fall back to the user's real workspace (cwd or ~/.config/opmagent/opmagent.toml): a config is
    passed explicitly, or comes from OPMAGENT_CONFIG set by the test."""
    monkeypatch.setattr(cfgmod, "DEFAULT_PATHS", [Path(os.environ.get("OPMAGENT_CONFIG", ""))])


@pytest.fixture
def cfg(tmp_path):
    c = Config(database=tmp_path / "opm.db", file_store=tmp_path / "files")
    db.init_db(c.database)
    for sub in ("inbox", "qc"):
        (c.file_store / sub).mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def hello_deck(cfg):
    """The hello deck: its four demo models approved and its initial state accepted, as a person would."""
    pytest.importorskip("opm.simulators")
    from opmagent.deck.init_review import accept, state
    from opmagent.hello.run import hello, latest_demo
    from opmagent.servers.data_server import DataService
    hello(cfg, log=lambda *_: None)
    rv = DataService("reviewer", cfg)
    for kind, m in latest_demo(rv.con).items():
        rid = rv.get_qc_report(kind=kind, version_id=m["version_id"])["qc_report"]["id"]
        rv.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid)
    deck = hello(cfg, log=lambda *_: None)["deck"]["deck_id"]
    if state(rv.con, deck) == "waiting":
        accept(cfg, rv.con, deck, "tester")
    return deck


@pytest.fixture
def hello_run(cfg, hello_deck):
    """A finished run of the hello deck (through the run agent's server)."""
    from opmagent.servers.sim_server import SimService
    from opmagent.servers.sim_server import wrap as swrap
    sim = SimService(cfg)
    a = swrap(sim, "run_simulation")(deck_id=hello_deck)
    assert a["ok"], a
    done = swrap(sim, "wait")(run_id=a["run_id"], timeout_s=300)
    assert done["ok"], done
    return a["run_id"]


@pytest.fixture
def qc_file(cfg):
    p = cfg.file_store / "qc" / "report.html"
    p.write_text("<html>qc</html>")
    return p


# A small, physically consistent black-oil fluid (METRIC)
PVTO = [
    # rs, p, bo, mu, sat
    (20, 50, 1.10, 1.20, 1), (20, 150, 1.09, 1.30, 0),
    (60, 120, 1.20, 0.90, 1), (60, 250, 1.18, 1.00, 0),
    (100, 200, 1.30, 0.70, 1), (100, 300, 1.28, 0.75, 0), (100, 400, 1.26, 0.80, 0),
]
PVDG = [(50, 0.025, 0.013), (150, 0.008, 0.016), (300, 0.0045, 0.022), (400, 0.0038, 0.025)]


def fluid_rows():
    return {
        "pvto": [dict(rs=r, pressure=p, bo=b, mu_o=m, is_saturated=s) for r, p, b, m, s in PVTO],
        "pvdg": [dict(pressure=p, bg=b, mu_g=m) for p, b, m in PVDG],
        "pvtw": [dict(p_ref=200, bw=1.02, cw=4.5e-5, mu_w=0.4)],
        "density": [dict(oil=850, water=1020, gas=0.9)],
    }
