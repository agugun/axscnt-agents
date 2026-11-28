"""The review workbench by model kind (docs/design/geo-review.md, phase 4).

Each reviewable kind has a views module with the same interface:

  FIGS, ANCHOR_KEYS
  load_version(cfg, con, version_id) -> data       the model as stored (with its QC findings)
  meta(data), figs(data)                           what the workbench offers for it
  view_args(data)                                  each view's arguments and their choices (generic controls)
  check_spec(spec, data, base=None)                a view spec checked against the model
  check_anchor(anchor)
  figure(spec, data, pins=(), base=None)           plotly figure; each trace's meta.role drives locate()
  locate(spec, data, role, point) -> anchor
  describe(anchor, data), summarize(spec, data, base=None)
  metrics(data), comparable(new, old), diff_extra(new, old)   the version diff

geo and res also have proposals and what-if previews; well is reviewed only (its changes are data edits, which go
to the data manager as requests). fluid has no views: the workbench shows its QC report, comments and the decision,
and the fluid modeler answers comments in its next version (no threads).
"""
from __future__ import annotations

from importlib import import_module

from .core.errors import INVALID_INPUT, ToolError

KINDS = ("geo", "res", "well")      # review threads with the modeler (replies, live sessions)
CHANGEABLE = ("geo", "res")          # proposals and previews
WORKBENCH = ("fluid",) + KINDS       # the review workbench (fluid: its QC report, comments and the decision only)


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"review threads are for {KINDS} models, not {kind!r}")
    return kind


def check_workbench_kind(kind: str) -> str:
    if kind not in WORKBENCH:
        raise ToolError(INVALID_INPUT, f"the review workbench is for {WORKBENCH} models, not {kind!r}")
    return kind


def views(kind: str):
    return import_module(f".{check_workbench_kind(kind)}.review_views", __package__)


def previews(kind: str):
    if kind not in CHANGEABLE:
        raise ToolError(INVALID_INPUT, f"{kind} models have no what-if previews (their changes are data edits)")
    return import_module(f".{kind}.preview", __package__)


def build_from_proposal(cfg, kind: str, proposal_id: int, **kw) -> dict:
    if kind not in CHANGEABLE:
        raise ToolError(INVALID_INPUT, f"{kind} models have no proposals")
    return import_module(f".{kind}.proposal_build", __package__).build_from_proposal(cfg, proposal_id, **kw)


def preview_row(con, preview_id: int) -> dict:
    from .geo.preview import get
    return get(con, preview_id)


def load_preview(cfg, con, preview_id: int) -> dict:
    """A preview of any kind, loaded as its kind's views draw it."""
    return previews(preview_row(con, preview_id)["model_kind"]).load(cfg, con, preview_id)
