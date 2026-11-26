"""Build a res version from an accepted review proposal (the review workbench, phase 4): the base version's spec with
the proposal's merge patch, on the same geo version, from the same data source; the proposal is marked built."""
from __future__ import annotations

from ..core import db, models
from ..core import proposals as props


def build_from_proposal(cfg, proposal_id: int, **_kw) -> dict:
    from .pipeline import build_res_model
    con = db.connect(cfg.database)
    try:
        p = props.accepted_for_build(con, proposal_id, "res")
        h = models.header(con, "res", p["base_version_id"])
        sj = h.get("spec_json") or {}
        who = "the engineer" if p["author_role"] == "reviewer" else "the res modeler"
        src = [r[0] for r in con.execute("SELECT file_id FROM res_version_sources WHERE version_id = ?",
                                         (p["base_version_id"],))]
        r = build_res_model(cfg, spec=p["spec"], name=h["model_name"], geo_version_id=h["geo_version_id"],
                            description=f"res proposal {proposal_id}", source_file_ids=src,
                            data_source=sj.get("data_source"), proposal_id=proposal_id,
                            extra_assumptions=[f"from res proposal {proposal_id} on v{h['version']} (by {who}, accepted "
                                               f"by {p['reviewer']}): {props.describe_changes(p['changes'])}"])
        if r.get("version_id"):
            props.mark_built(con, proposal_id, r["version_id"])
            r["proposal_id"] = proposal_id
            r["spoken_summary"] = (r.get("spoken_summary", "") + f" Built from proposal {proposal_id}; answer its "
                                   "threads with this version.").strip()
        return r
    finally:
        con.close()
