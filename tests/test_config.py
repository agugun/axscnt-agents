"""Configuration: runs use a release Flow, never the Debug build."""
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
