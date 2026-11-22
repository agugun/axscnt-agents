"""Where the results viewer finds ResInsight: the configured path, PATH, then /opt/ResInsight and ~/.local/opt."""
from pathlib import Path

from opmagent import viewer


def _launcher(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_configured_path_wins_and_must_exist(tmp_path):
    exe = _launcher(tmp_path / "custom" / "bin" / "resinsight")
    assert viewer.find_resinsight(str(exe)) == str(exe)
    assert viewer.find_resinsight(str(tmp_path / "missing" / "resinsight")) is None   # no silent fallback


def test_path_then_standard_places(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    opt = tmp_path / "opt" / "ResInsight" / "bin" / "resinsight"
    monkeypatch.setattr(viewer, "STANDARD_PLACES", (opt, tmp_path / "home" / ".local/opt/ResInsight/bin/resinsight"))
    assert viewer.find_resinsight() is None
    _launcher(opt)
    assert viewer.find_resinsight() == str(opt)                    # /opt/ResInsight
    on_path = _launcher(tmp_path / "bin" / "resinsight")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert viewer.find_resinsight() == str(on_path)                # PATH before /opt


def test_setup_no_longer_looks_in_the_workspace(tmp_path, monkeypatch):
    from opmagent.cli import main
    monkeypatch.setenv("PATH", str(tmp_path / "empty") + ":" + __import__("os").environ["PATH"])
    monkeypatch.setattr(viewer, "STANDARD_PLACES", ())
    ws = tmp_path / "ws"
    _launcher(ws / "ResInsight" / "bin" / "resinsight")             # the old place, inside the workspace
    main(["setup", str(ws)])
    line = next(ln for ln in (ws / "opmagent.toml").read_text().splitlines() if "resinsight_bin" in ln)
    assert line.startswith("#") and "/ws/ResInsight" not in line
