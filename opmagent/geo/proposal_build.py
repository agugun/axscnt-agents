"""Build a geo version from an accepted review proposal (geo review workbench, phase 2).

The new version is the base version's spec with the proposal's merge patch applied, from the same data: the study's
tables and logs if the base came from a study, else the tops and log files the base version recorded. The version's
assumptions name the proposal, its spec records proposal_id, and the proposal is marked built (a message in each
thread it answers). The modeler then answers those threads with the new version.
"""
from __future__ import annotations

from ..core import db, models
from ..core import proposals as props
from ..core.errors import INVALID_INPUT, ToolError


def build_from_proposal(cfg, proposal_id: int, *, surveys=None) -> dict:
    from .pipeline import build_geo_model
    con = db.connect(cfg.database)
    try:
        p = props.accepted_for_build(con, proposal_id, "geo")
        h = models.header(con, "geo", p["base_version_id"])
        sj = h.get("spec_json") or {}
        spec = p["spec"]
        extra = [f"from geo proposal {proposal_id} on v{h['version']} (accepted by {p['reviewer']}): "
                 f"{props.describe_changes(p['changes'])}"]
        common = dict(spec=spec, name=h["model_name"], description=f"geo proposal {proposal_id}",
                      extra_assumptions=extra, proposal_id=proposal_id)
        study = (sj.get("data_source") or {}).get("study")
        if study:
            from ..data import store
            r = build_geo_model(cfg, tops=store.tops(con, study), logs=store.logs(con, study, spec.get("curves")),
                                surveys=store.survey_xyz(con, study), data_source={"study": study},
                                source_file_ids=store.source_file_ids(con, study, store.FOR["geo"]), **common)
        else:
            files = [dict(r) for r in con.execute(
                "SELECT f.id, f.path, f.kind, f.sha256 FROM geo_version_sources s JOIN files f ON f.id = s.file_id "
                "WHERE s.version_id = ? ORDER BY f.id", (p["base_version_id"],))]
            from pathlib import Path

            from ..core.files import sha256_of
            changed = [f["path"] for f in files if f["kind"] in ("tops", "well_log")
                       and not (Path(f["path"]).is_file() and sha256_of(f["path"]) == f["sha256"])]
            if changed:
                raise ToolError(INVALID_INPUT, f"the base version's input files changed or are gone: {changed}")
            tops = [f for f in files if f["kind"] == "tops"]
            logs = [f for f in files if f["kind"] == "well_log"]
            if len(tops) != 1 or not logs:
                raise ToolError(INVALID_INPUT, f"geo v{h['version']} doesn't record one tops file and its logs; "
                                               "build it from a study, or with `opmagent geo build` by hand")
            r = build_geo_model(cfg, tops=tops[0]["path"], logs=[f["path"] for f in logs], surveys=surveys,
                                source_file_ids=[tops[0]["id"]] + [f["id"] for f in logs], **common)
        if r.get("version_id"):
            props.mark_built(con, proposal_id, r["version_id"])
            r["proposal_id"] = proposal_id
            r["spoken_summary"] = (r.get("spoken_summary", "") + f" Built from proposal {proposal_id}; answer its "
                                   "threads with this version.").strip()
        return r
    finally:
        con.close()
