"""File formats the data manager keeps as files, and how to read them.

Data another application opens directly (LAS logs, SEG-Y, surfaces, grids, documents, images) stays a file, as it
came, in the study's managed folder <study>/data/<category>/. The database holds its catalog entry (data_files)
and, where the format has an inspector, what can be said about it without opening it again (a LAS file's well,
curves, units, depth range and statistics: data_log_curves). Reading goes through `read`, which the study data
layer (opmagent.data.store) calls after checking the file hasn't changed since it was loaded.

Add a format: one entry in FORMATS with its extensions, category, file kind and, optionally, inspect / read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from ..core.errors import INVALID_INPUT, ToolError


@dataclass(frozen=True)
class Format:
    name: str
    extensions: tuple[str, ...]
    category: str                      # the folder under <study>/data/
    file_kind: str                     # files.kind
    inspect: Callable[[Path], dict] | None = None    # -> catalog metadata
    read: Callable[[Path], object] | None = None     # -> the data (a DataFrame for logs)
    readers: tuple[str, ...] = field(default=())     # other software that opens it


# ---------------------------------------------------------------- LAS
def _las(path: Path):
    try:
        import lasio
    except ImportError as e:
        raise ToolError(INVALID_INPUT, "lasio is not installed: pip install lasio") from e
    return lasio.read(str(path))


def las_inspect(path: Path) -> dict:
    """Well, depth index and per-curve unit, description, samples, depth range and min / mean / max."""
    las = _las(path)
    df = las.df()
    depth = las.curves[0]
    curves = []
    for c in las.curves[1:]:
        s = df[c.mnemonic].dropna()
        curves.append({"mnemonic": c.mnemonic.upper(), "unit": c.unit or None, "description": c.descr or None,
                       "samples": int(s.size),
                       "top": float(s.index.min()) if s.size else None, "base": float(s.index.max()) if s.size else None,
                       "min": float(s.min()) if s.size else None, "mean": float(s.mean()) if s.size else None,
                       "max": float(s.max()) if s.size else None})
    head = {i.mnemonic: str(i.value) for i in las.well if str(i.value).strip()}
    return {"well": str(las.well.WELL.value or path.stem).strip(),
            "depth": {"mnemonic": depth.mnemonic.upper(), "unit": depth.unit or None,
                      "top": float(df.index.min()) if len(df) else None,
                      "base": float(df.index.max()) if len(df) else None, "step": _step(las)},
            "curves": curves, "header": head}


def _step(las):
    try:
        v = float(las.well.STEP.value)
        return v if v == v else None
    except (AttributeError, TypeError, ValueError):
        return None


def las_read(path: Path) -> pd.DataFrame:
    """The log table: the depth index first, then one column per curve (mnemonics upper case)."""
    df = _las(path).df().reset_index()
    df.columns = [str(c).upper() for c in df.columns]
    return df


# ---------------------------------------------------------------- SEG-Y (catalogued; header if segyio is there)
def segy_inspect(path: Path) -> dict:
    try:
        import segyio
    except ImportError:
        return {}
    try:
        with segyio.open(str(path), ignore_geometry=True) as f:
            return {"traces": int(f.tracecount), "samples": int(len(f.samples)),
                    "sample_interval_ms": float(segyio.tools.dt(f) / 1000.0)}
    except Exception:  # noqa: BLE001  (catalogue it even if the header can't be read)
        return {}


FORMATS = [
    Format("las", (".las",), "logs", "well_log", las_inspect, las_read, ("Techlog", "Petrel", "ResInsight", "lasio")),
    Format("segy", (".sgy", ".segy"), "seismic", "other_input", segy_inspect, None, ("Petrel", "OpendTect", "segyio")),
    Format("surface", (".irap", ".irapbin", ".zmap", ".gri", ".ts"), "surfaces", "surface",
           readers=("Petrel", "RMS", "xtgeo")),
    Format("grid", (".egrid", ".grdecl", ".init", ".unrst", ".roff"), "grids", "grid",
           readers=("ResInsight", "Petrel", "resdata")),
    Format("document", (".pdf", ".docx", ".doc", ".pptx", ".txt", ".md"), "documents", "other_input"),
    Format("image", (".png", ".jpg", ".jpeg", ".tif", ".tiff"), "images", "core"),
]
BY_EXT = {e: f for f in FORMATS for e in f.extensions}
BY_NAME = {f.name: f for f in FORMATS}
TABLE_EXT = (".xlsx", ".xlsm", ".xls", ".csv")


def format_of(path: Path) -> Format | None:
    return BY_EXT.get(Path(path).suffix.lower())


def read(fmt: str, path: Path):
    f = BY_NAME.get(fmt)
    if not f or not f.read:
        raise ToolError(INVALID_INPUT, f"no reader for {fmt} files yet; it is kept as a file for "
                                       f"{', '.join(f.readers) if f and f.readers else 'other software'}")
    return f.read(Path(path))
