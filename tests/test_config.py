"""Configuration: runs use a release Flow, never the Debug build."""
from pathlib import Path

import pytest

from opmagent.core import config


def _toml(tmp_path, flow_bin, debug_flow):
    p = tmp_path / "opmagent.toml"
    p.write_text(f'[paths]\ndatabase = "{tmp_path}/opm.db"\nfile_store = "{tmp_path}/files"\nflow_bin = "{flow_bin}"\n'
                 f'[debug]\nopm_debug_flow = "{debug_flow}"\n')
    return p


def test_flow_bin_may_not_be_the_debug_build(tmp_path):
    dbg = tmp_path / "build" / "flow"
    dbg.parent.mkdir()
    dbg.write_text("#!/bin/sh\n")
    dbg.chmod(0o755)
    with pytest.raises(ValueError, match="Debug build"):
        config.load(str(_toml(tmp_path, dbg, dbg)))
    cfg = config.load(str(_toml(tmp_path, "flow", dbg)))          # release flow for runs, Debug build kept for VS Code
    assert cfg.flow_bin == "flow" and cfg.opm_debug_flow == dbg


def test_default_workspace_config(tmp_path, monkeypatch):
    """No --config, no OPMAGENT_CONFIG, none in this folder: the default workspace's (~/.config/opmagent)."""
    toml = _toml(tmp_path, "flow", tmp_path / "none")
    user = tmp_path / "home" / ".config" / "opmagent" / "opmagent.toml"
    user.parent.mkdir(parents=True)
    user.symlink_to(toml)
    monkeypatch.setattr(config, "USER_CONFIG", user)
    monkeypatch.setattr(config, "DEFAULT_PATHS", [Path(""), Path("opmagent.toml"), user])
    monkeypatch.delenv("OPMAGENT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path / "home")
    assert config.find_path(None) == toml.resolve()
    assert config.load(None).database == tmp_path / "opm.db"
    other = _toml(tmp_path / "home", "flow", tmp_path / "none")    # one in this folder wins
    assert config.find_path(None) == other.resolve()
