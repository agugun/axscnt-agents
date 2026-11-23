"""Load opmagent.toml (one config shared by all servers)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

USER_CONFIG = Path.home() / ".config" / "opmagent" / "opmagent.toml"   # the default workspace (create_workspace.sh links it)
DEFAULT_PATHS = [
    Path(os.environ.get("OPMAGENT_CONFIG", "")),
    Path.cwd() / "opmagent.toml",
    USER_CONFIG,
]


def _candidates(path) -> list[Path]:
    if path:
        return [Path(path)]
    if DEFAULT_PATHS and DEFAULT_PATHS[-1] == USER_CONFIG:   # read OPMAGENT_CONFIG and the cwd now, not at import
        return [p for p in (Path(os.environ.get("OPMAGENT_CONFIG", "")), Path.cwd() / "opmagent.toml", USER_CONFIG)
                if str(p) not in ("", ".")]
    return [p for p in DEFAULT_PATHS if str(p) not in ("", ".")]


@dataclass
class Config:
    database: Path
    file_store: Path
    flow_bin: str = "flow"
    unit_system: str = "METRIC"
    eos: str = "PR"
    volume_shift: bool = True
    pseudo_components: int = 6
    tolerances: dict = field(default_factory=lambda: {"psat": 0.02, "bo": 0.02, "rs": 0.03, "mu_o": 0.10})
    max_parallel: int = 2
    opm_source_root: Path | None = None
    opm_debug_flow: Path | None = None
    vscode_bin: str = "code"
    auto_approve: bool = False          # [study] auto_approve: models that pass their validator are approved
    resinsight_bin: str | None = None   # [viewer] resinsight_bin: ResInsight launcher (bin/resinsight)
    qc_report: str = "notebook"         # [qc] report: "notebook" (executed Jupyter template) or "html" (static report)
    qc_timeout_s: int = 600             # [qc] timeout_s: per-cell timeout for QC notebooks
    hm_parallel: int | None = None      # [hm] parallel: variants at once (default: cores / (2 x threads), at least 1)
    hm_threads: int = 2                 # [hm] threads: OpenMP threads per variant run
    hm_min_free_mb: int = 1024          # [hm] min_free_mb: memory left free when starting a variant
    hm_case_memory_mb: int | None = None  # [hm] case_memory_mb: memory one variant needs (default: from the grid)
    hm_case_timeout_s: int = 3600       # [hm] case_timeout_s: a variant run longer than this is stopped and failed
    hm_min_free_disk_mb: int = 2048     # [hm] min_free_disk_mb: disk left free where the variants write
    hm_keep_grid_output: bool = False   # [hm] keep_grid_output: keep each variant's restart and grid files

    @property
    def inbox(self) -> Path:
        return self.file_store / "inbox"

    @property
    def studies_dir(self) -> Path:
        return self.file_store / "studies"


def _expand(p: str | None) -> Path | None:
    return Path(os.path.expanduser(p)) if p else None


def find_path(path: str | os.PathLike | None = None) -> Path | None:
    """The config file load() would read, or None."""
    return next((p.resolve() for p in _candidates(path) if p.is_file()), None)


def load(path: str | os.PathLike | None = None) -> Config:
    for p in _candidates(path):
        if p.is_file():
            data = tomllib.loads(p.read_text())
            break
    else:
        raise FileNotFoundError(
            "opmagent.toml not found; copy opmagent.example.toml and set OPMAGENT_CONFIG or place it in the working directory"
        )
    paths, d, sim, dbg, st, vw = (data.get(k, {}) for k in ("paths", "defaults", "simulation", "debug", "study", "viewer"))
    qc = data.get("qc", {})
    hm = data.get("hm", {})
    if qc.get("report", "notebook") not in ("notebook", "html"):
        raise ValueError('[qc] report must be "notebook" or "html"')
    cfg = Config(
        database=_expand(paths["database"]),
        file_store=_expand(paths["file_store"]),
        flow_bin=paths.get("flow_bin", "flow"),
        unit_system=d.get("unit_system", "METRIC"),
        eos=d.get("eos", "PR"),
        volume_shift=d.get("volume_shift", True),
        pseudo_components=d.get("pseudo_components", 6),
        tolerances={**Config.__dataclass_fields__["tolerances"].default_factory(), **data.get("tolerances", {})},
        max_parallel=sim.get("max_parallel", 2),
        opm_source_root=_expand(dbg.get("opm_source_root")),
        opm_debug_flow=_expand(dbg.get("opm_debug_flow")),
        vscode_bin=dbg.get("vscode_bin", "code"),
        auto_approve=bool(st.get("auto_approve", False)),
        resinsight_bin=os.path.expanduser(vw["resinsight_bin"]) if vw.get("resinsight_bin") else None,
        qc_report=qc.get("report", "notebook"),
        qc_timeout_s=int(qc.get("timeout_s", 600)),
        hm_parallel=int(hm["parallel"]) if hm.get("parallel") else None,
        hm_threads=int(hm.get("threads", 2)),
        hm_min_free_mb=int(hm.get("min_free_mb", 1024)),
        hm_case_memory_mb=int(hm["case_memory_mb"]) if hm.get("case_memory_mb") else None,
        hm_case_timeout_s=int(hm.get("case_timeout_s", 3600)),
        hm_min_free_disk_mb=int(hm.get("min_free_disk_mb", 2048)),
        hm_keep_grid_output=bool(hm.get("keep_grid_output", False)),
    )
    _check_release_flow(cfg)
    return cfg


def _check_release_flow(cfg: Config) -> None:
    """Runs (agents, studies, round trips) use [paths] flow_bin and must be a release build: a Debug build of OPM is
    many times slower. The Debug build ([debug] opm_debug_flow) is only for debug_simulation in VS Code."""
    if not cfg.opm_debug_flow:
        return
    import shutil
    exe = shutil.which(cfg.flow_bin) or cfg.flow_bin
    try:
        same = Path(exe).expanduser().resolve() == Path(cfg.opm_debug_flow).expanduser().resolve()
    except OSError:
        same = False
    if same:
        raise ValueError(f"[paths] flow_bin points at the Debug build ({cfg.opm_debug_flow}); runs need a release "
                         "Flow (e.g. flow from the OPM package). Keep the Debug build in [debug] opm_debug_flow only.")
