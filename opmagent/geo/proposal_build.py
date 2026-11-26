"""Build a geo version from an accepted review proposal (geo review workbench, phase 2).

The new version is the base version's spec with the proposal's merge patch applied, from the same data: the study's
tables and logs if the base came from a study, else the tops and log files the base version recorded (preview.py's
base_inputs, which a what-if preview uses too, so a preview and the build from it read the same data). The version's
assumptions name the proposal, its spec records proposal_id, and the proposal is marked built (a message in each
thread it answers). The modeler then answers those threads with the new version.
"""
from __future__ import annotations

from ..core import db, models
from ..core import proposals as props


def build_from_proposal(cfg, proposal_id: int, *, surveys=None) -> dict:
    from .pipeline import build_geo_model
    from .preview import base_inputs
    con = db.connect(cfg.database)
    try:
        p = props.accepted_for_build(con, proposal_id, "geo")
        h = models.header(con, "geo", p["base_version_id"])
        spec = p["spec"]
        who = "the engineer" if p["author_role"] == "reviewer" else "the geo modeler"
        extra = [f"from geo proposal {proposal_id} on v{h['version']} (by {who}, accepted by {p['reviewer']}): "
                 f"{props.describe_changes(p['changes'])}"]
        inp = base_inputs(con, p["base_version_id"], surveys=surveys)
        common = dict(spec=spec, name=h["model_name"], description=f"geo proposal {proposal_id}",
                      extra_assumptions=extra, proposal_id=proposal_id, source_file_ids=inp["source_file_ids"])
        if inp["study"]:
            r = build_geo_model(cfg, tops=inp["tops"], logs=inp["logs"](spec.get("curves")), surveys=inp["surveys"],
                                data_source={"study": inp["study"]}, **common)
        else:
            r = build_geo_model(cfg, tops=inp["tops_path"], logs=inp["log_paths"], surveys=surveys, **common)
        if r.get("version_id"):
            props.mark_built(con, proposal_id, r["version_id"])
            r["proposal_id"] = proposal_id
            r["spoken_summary"] = (r.get("spoken_summary", "") + f" Built from proposal {proposal_id}; answer its "
                                   "threads with this version.").strip()
        return r
    finally:
        con.close()
