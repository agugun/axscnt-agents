"""Append per-kind lifecycle triggers, data locks, views and indexes to schema.sql."""
import sys
kinds = {
  "fluid": ("fluid_model_versions","fluid_model_id",
            ["fluid_version_sources","pvto","pvdo","pvdg","pvtg","pvtw","density","rsvd","pbvd"]),
  "geo":   ("geo_model_versions","geo_model_id",
            ["geo_version_sources","geo_properties","faults","fault_multipliers","rock",
             "swof","sgof","contacts","aquifers","aquifer_connections"]),
  "well":  ("well_model_versions","well_model_id",
            ["well_version_sources","well_groups","wells","well_trajectory","well_completions",
             "well_events","well_econ_limits","group_controls","vfp_tables"]),
}
out=[]
for k,(vt,mid,children) in kinds.items():
    out.append(f"""
-- ======================= {k.upper()} lifecycle =======================
CREATE TRIGGER {k}_insert_as_draft BEFORE INSERT ON {vt}
WHEN NEW.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'new {k} versions must start as draft'); END;

CREATE TRIGGER {k}_version_autonumber AFTER INSERT ON {vt}
WHEN NEW.version = 0
BEGIN
    UPDATE {vt} SET version = (SELECT COALESCE(MAX(version),0) + 1 FROM {vt}
                                WHERE {mid} = NEW.{mid} AND id <> NEW.id)
     WHERE id = NEW.id;
END;

CREATE TRIGGER {k}_status_rules BEFORE UPDATE ON {vt}
BEGIN
    SELECT RAISE(ABORT, '{k} version is approved/rejected and immutable; create a new version')
     WHERE OLD.status IN ('approved','rejected');
    SELECT RAISE(ABORT, '{k} version is qc_ready (frozen for review); move back to draft to edit')
     WHERE OLD.status = 'qc_ready' AND NEW.status = 'qc_ready'
       AND NOT (OLD.qc_ready_at IS NULL AND NEW.qc_ready_at IS NOT NULL);  -- allow the timestamp stamp only
    SELECT RAISE(ABORT, 'invalid status transition for {k} version')
     WHERE NOT ((OLD.status = 'draft'    AND NEW.status IN ('draft','qc_ready')) OR
                (OLD.status = 'qc_ready' AND NEW.status IN ('draft','qc_ready','approved','rejected')));
    SELECT RAISE(ABORT, 'qc_ready requires qc_json and a QC report file')
     WHERE NEW.status = 'qc_ready' AND (NEW.qc_json IS NULL OR NEW.qc_report_file_id IS NULL);
    SELECT RAISE(ABORT, 'approve/reject only through the approvals table (human review)')
     WHERE NEW.status IN ('approved','rejected')
       AND NOT EXISTS (SELECT 1 FROM approvals
                        WHERE model_kind = '{k}' AND version_id = NEW.id
                          AND decision = NEW.status);
END;

CREATE TRIGGER {k}_stamp_qc_ready AFTER UPDATE OF status ON {vt}
WHEN NEW.status = 'qc_ready'
BEGIN UPDATE {vt} SET qc_ready_at = datetime('now') WHERE id = NEW.id AND qc_ready_at IS NULL; END;

CREATE TRIGGER {k}_reset_qc_on_draft AFTER UPDATE OF status ON {vt}
WHEN NEW.status = 'draft' AND OLD.status = 'qc_ready'
BEGIN UPDATE {vt} SET qc_ready_at = NULL WHERE id = NEW.id; END;

CREATE TRIGGER {k}_no_delete_after_draft BEFORE DELETE ON {vt}
WHEN OLD.status <> 'draft'
BEGIN SELECT RAISE(ABORT, 'only draft {k} versions can be deleted'); END;

CREATE VIEW latest_approved_{k} AS
SELECT m.name AS model_name, v.*
  FROM {vt} v JOIN {k}_models m ON m.id = v.{mid}
 WHERE v.status = 'approved'
   AND v.version = (SELECT MAX(version) FROM {vt}
                     WHERE {mid} = v.{mid} AND status = 'approved');

CREATE VIEW pending_review_{k} AS
SELECT m.name AS model_name, v.id, v.version, v.qc_ready_at, v.qc_report_file_id, v.qc_json
  FROM {vt} v JOIN {k}_models m ON m.id = v.{mid}
 WHERE v.status = 'qc_ready';

CREATE INDEX idx_{k}_versions ON {vt}({mid}, status);
""")
    lock = f"(SELECT status FROM {vt} WHERE id = {{r}}.version_id) IN ('qc_ready','approved','rejected')"
    for t in children:
        for op,row in (("INSERT","NEW"),("UPDATE","NEW"),("DELETE","OLD")):
            out.append(f"CREATE TRIGGER lock_{t}_{op.lower()} BEFORE {op} ON {t}\n"
                       f"WHEN {lock.format(r=row)}\n"
                       f"BEGIN SELECT RAISE(ABORT, '{t}: {k} version is not draft; data is frozen'); END;\n")
out.append("""
CREATE INDEX idx_wcomp  ON well_completions(version_id, well_name, event_date);
CREATE INDEX idx_wevent ON well_events(version_id, well_name, event_date);
CREATE INDEX idx_gctrl  ON group_controls(version_id, group_name, event_date);
CREATE INDEX idx_approvals ON approvals(model_kind, version_id);
CREATE INDEX idx_decks_models ON decks(fluid_version_id, geo_version_id, well_version_id);
""")
open(sys.argv[1],"a").write("\n".join(out))
