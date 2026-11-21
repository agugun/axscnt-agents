"""Load opmagent.toml (one config shared by all servers)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATHS = [
    Path(os.environ.get("OPMAGENT_CONFIG", "")),
    Path.cwd() / "opmagent.toml",
    Path.home() / ".config" / "opmagent" / "opmagent.toml",
]


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

    @property
    def inbox(self) -> Path:
        return self.file_store / "inbox"

    @property
    def studies_dir(self) -> Path:
        return self.file_store / "studies"


def _expand(p: str | None) -> Path | None:
    return Path(os.path.expanduser(p)) if p else None


def load(path: str | os.PathLike | None = None) -> Config:
    candidates = [Path(path)] if path else [p for p in DEFAULT_PATHS if str(p)]
    for p in candidates:
        if p.is_file():
            data = tomllib.loads(p.read_text())
            break
    else:
        raise FileNotFoundError(
            "opmagent.toml not found; copy opmagent.example.toml and set OPMAGENT_CONFIG or place it in the working directory"
        )
    paths, d, sim, dbg, st, vw = (data.get(k, {}) for k in ("paths", "defaults", "simulation", "debug", "study", "viewer"))
    qc = data.get("qc", {})
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
    )
    return cfg
