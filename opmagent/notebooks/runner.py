"""Execute QC notebooks headlessly and export them to HTML.

Standard QC: the fixed template of the model kind (fluid_qc.ipynb, geo_qc.ipynb, shipped with the package)
gets a parameters cell injected (papermill style), runs in a fresh kernel of this Python interpreter, and is
saved per version:

    <file_store>/qc/<kind>_<model>_v<N>/
        payload.json          what the pipeline knew at build time (fluid: lab data, simulations, checks,
                              spec; geo: tops, upscaled logs, zones, spec, and logs.csv next to it;
                              well: gaps and assumptions, and history.csv, the measured history)
        <kind>_qc.ipynb       the executed notebook (code + outputs)
        <kind>_qc.html        the reviewer's copy (registered as the version's QC report; its sha256 is
                              what an approval is tied to)
        qc_summary.json       pass / warn / fail findings written by the notebook's last cell
        investigations/       free-form notebooks the agent writes to dig into a problem

Investigation notebooks start from a small header (parameters + data loading) and are then edited by
the agent or the engineer. They are evidence for the reviewer, never a replacement for the standard QC.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..core.files import sha256_of

TEMPLATE_DIR = Path(__file__).parent
FLUID_TEMPLATE = TEMPLATE_DIR / "fluid_qc.ipynb"
GEO_TEMPLATE = TEMPLATE_DIR / "geo_qc.ipynb"
WELL_TEMPLATE = TEMPLATE_DIR / "well_qc.ipynb"
RES_TEMPLATE = TEMPLATE_DIR / "res_qc.ipynb"
KINDS = ("fluid", "geo", "res", "well")
KERNEL_NAME = "opmagent-qc"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "x"


def qc_dir(file_store: Path, model_name: str, version: int, kind: str = "fluid") -> Path:
    return Path(file_store) / "qc" / f"{kind}_{slug(model_name)}_v{version}"


def template(kind: str) -> Path:
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"no QC notebook for {kind} models (only {', '.join(KINDS)})")
    # looked up at call time, so a test can swap a template
    return {"fluid": FLUID_TEMPLATE, "geo": GEO_TEMPLATE, "res": RES_TEMPLATE, "well": WELL_TEMPLATE}[kind]


def write_payload(folder: Path, payload: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "payload.json"
    p.write_text(json.dumps({"schema": 1, "created_at": f"{datetime.now():%Y-%m-%d %H:%M}", **payload},
                            indent=2, default=str))
    return p


# ---------------------------------------------------------------- execution
def _kernel_manager():
    """A kernel of *this* interpreter (the venv opmagent runs in), whatever kernels Jupyter has installed."""
    from jupyter_client.kernelspec import KernelSpecManager
    from jupyter_client.manager import KernelManager

    d = Path(tempfile.gettempdir()) / "opmagent-kernels"
    spec = d / KERNEL_NAME
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "kernel.json").write_text(json.dumps({
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "opmagent QC", "language": "python"}))
    ksm = KernelSpecManager(kernel_dirs=[str(d)], ensure_native_kernel=False)
    return KernelManager(kernel_name=KERNEL_NAME, kernel_spec_manager=ksm)


def _inject_parameters(nb, params: dict):
    """Insert an 'injected-parameters' cell after the cell tagged 'parameters' (as papermill does)."""
    import nbformat
    src = "# Parameters (injected)\n" + "".join(f"{k} = {v!r}\n" for k, v in params.items())
    cell = nbformat.v4.new_code_cell(src, metadata={"tags": ["injected-parameters"]})
    nb.cells = [c for c in nb.cells if "injected-parameters" not in c.get("metadata", {}).get("tags", [])]
    idx = next((i for i, c in enumerate(nb.cells) if "parameters" in c.get("metadata", {}).get("tags", [])), -1)
    nb.cells.insert(idx + 1, cell)
    return nb


def execute(nb_path: Path, out_path: Path, params: dict | None = None, *, cwd: Path | None = None,
            timeout: int = 600, show_code: bool = False) -> dict:
    """Run a notebook top to bottom in a fresh kernel, save it executed, export HTML next to it.
    A failing cell is kept in the saved notebook (with its traceback) and reported as ok=False."""
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError
    from nbconvert import HTMLExporter

    nb = nbformat.read(nb_path, as_version=4)
    if params:
        nb = _inject_parameters(nb, params)
    nb.metadata["kernelspec"] = {"name": KERNEL_NAME, "display_name": "opmagent QC", "language": "python"}
    cwd = Path(cwd or out_path.parent)
    client = NotebookClient(nb, km=_kernel_manager(), timeout=timeout, resources={"metadata": {"path": str(cwd)}})
    error = None
    try:
        client.execute()
    except CellExecutionError as e:
        error = str(e).strip().splitlines()[-1] if str(e).strip() else "cell failed"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nbformat.write(nb, out_path)
    exporter = HTMLExporter(exclude_input=not show_code, exclude_input_prompt=True, exclude_output_prompt=True)
    body, _ = exporter.from_notebook_node(nb)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(body)
    return {"ok": error is None, "error": error, "notebook": str(out_path), "html": str(html_path),
            "notebook_sha256": sha256_of(out_path), "html_sha256": sha256_of(html_path)}


# ---------------------------------------------------------------- standard QC
def run_fluid_qc(folder: Path, db_path: Path, version_id: int, *, timeout: int = 600) -> dict:
    return run_qc("fluid", folder, db_path, version_id, timeout=timeout)


def run_qc(kind: str, folder: Path, db_path: Path, version_id: int, *, timeout: int = 600) -> dict:
    """Execute the kind's standard template for one version folder (payload.json must exist)."""
    tpl = template(kind)
    payload = folder / "payload.json"
    if not payload.is_file():
        raise ToolError(NOT_FOUND, f"no QC payload in {folder}; rebuild the model to create one")
    summary = folder / "qc_summary.json"
    summary.unlink(missing_ok=True)
    r = execute(tpl, folder / f"{kind}_qc.ipynb", {
        "payload_path": str(payload), "db_path": str(db_path), "version_id": int(version_id),
        "summary_path": str(summary)}, cwd=folder, timeout=timeout)
    r["payload"] = str(payload)
    r["summary"] = json.loads(summary.read_text()) if summary.is_file() else None
    if r["ok"] and r["summary"] is None:
        r.update(ok=False, error="the notebook finished without writing qc_summary.json")
    return r


# ---------------------------------------------------------------- investigations
LOADERS = {
    "fluid": ("from opmagent.pvt import engine\n"
              "data = qc.load(payload_path, db_path, version_id)\n"
              "spec = qc.fluid_spec(data)   # tuned EOS fluid; e.g. engine.simulate_dl(spec, [...])\n"),
    "geo": ("import numpy as np\n"
            "data = qc.load(payload_path, db_path, version_id)\n"
            "arr, logs, tops = data['arrays'], data['logs'], data['tops']   # arr['PORO'][k, j, i]; logs per sample\n"
            "ld = data['layer_depths']   # (nz+1, ny, nx) layer boundaries at cell centres; qc.fig_section, qc.fig_well ...\n"),
    "res": ("data = qc.load(payload_path, db_path, version_id)\n"
            "sw, sg, grid = data['swof'], data['sgof'], data['grid']   # grid['PORV'][k, j, i], grid['SATNUM'] ... or None\n"
            "t = data['tables']   # rock, contacts, aquifers, aquifer_connections; qc.end_points(data), qc.region_volumes\n"),
    "well": ("data = qc.load(payload_path, db_path, version_id)\n"
             "ev, comp, traj, measured = data['events'], data['completions'], data['trajectory'], data['measured']\n"
             "geo = data['geo']   # the geo version the wells sit in (or None); qc.place(data) puts perforations in cells\n"),
}
LIBS = {"fluid": "qc_lib", "geo": "geo_qc_lib", "res": "res_qc_lib", "well": "well_qc_lib"}


def create_investigation(folder: Path, db_path: Path, version_id: int, purpose: str, kind: str = "fluid") -> Path:
    """A starter notebook in <folder>/investigations/: purpose, parameters, data loading. Add cells after it."""
    import nbformat
    if not purpose or not purpose.strip():
        raise ToolError(INVALID_INPUT, "say what the investigation is for (purpose)")
    if not (folder / "payload.json").is_file():
        raise ToolError(NOT_FOUND, f"no QC payload in {folder}; rebuild the model to create one")
    inv = folder / "investigations"
    inv.mkdir(parents=True, exist_ok=True)
    n = 1 + max((int(m.group(1)) for p in inv.glob("*.ipynb") if (m := re.match(r"(\d+)_", p.name))), default=0)
    path = inv / f"{n:02d}_{slug(purpose)[:40]}.ipynb"
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_markdown_cell(f"# Investigation {n}: {purpose.strip()}\n\n"
                                      f"Created {datetime.now():%Y-%m-%d %H:%M}. Read only: load, compute, plot. "
                                      "Model changes go through a new build, never through this notebook."),
        nbformat.v4.new_code_cell(f"payload_path = {str(folder / 'payload.json')!r}\ndb_path = {str(db_path)!r}\n"
                                  f"version_id = {int(version_id)}", metadata={"tags": ["parameters"]}),
        nbformat.v4.new_code_cell(
            "import plotly.io as pio\n"
            "pio.renderers.default = 'notebook'\n"
            f"from opmagent.notebooks import {LIBS[kind]} as qc\n"
            + LOADERS[kind] + "qc.header(data)"),
        nbformat.v4.new_markdown_cell("## Analysis"),
        nbformat.v4.new_code_cell("# add the analysis here"),
        nbformat.v4.new_markdown_cell("## Conclusion\n\n_What was found and what it means for the model._"),
    ]
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nbformat.write(nb, path)
    return path


def run_notebook(path: Path, qc_root: Path, *, timeout: int = 600, show_code: bool = True) -> dict:
    """Execute an investigation notebook in place (it must live under the QC folder) and export HTML."""
    path, qc_root = Path(path).expanduser().resolve(), Path(qc_root).expanduser().resolve()
    if path.suffix != ".ipynb" or not path.is_file():
        raise ToolError(NOT_FOUND, f"notebook not found: {path}")
    if qc_root not in path.parents:
        raise ToolError(INVALID_INPUT, f"only notebooks under {qc_root} can be run")
    return execute(path, path, cwd=path.parent, timeout=timeout, show_code=show_code)
