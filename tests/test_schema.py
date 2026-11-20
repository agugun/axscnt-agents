"""Approval gate and integrity rules enforced by the SQLite schema."""
import sqlite3

import pytest

from opmagent.core import db


@pytest.fixture
def con(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    c = db.connect(path)
    c.execute("insert into files(path,sha256,kind) values('qc1','a','qc_report')")
    c.execute("insert into files(path,sha256,kind) values('qc2','b','qc_report')")
    for k in ("fluid", "geo", "well"):
        c.execute(f"insert into {k}_models(name) values('{k}A')")
    c.execute("insert into fluid_model_versions(fluid_model_id,unit_system,phases) values(1,'METRIC','OIL,WATER,GAS')")
    c.execute("insert into geo_model_versions(geo_model_id,unit_system,grid_type,nx,ny,nz,dx,dy,dz,tops) values(1,'METRIC','cartesian',10,10,3,100,100,10,2000)")
    c.execute("insert into well_model_versions(well_model_id,unit_system,geo_version_id) values(1,'METRIC',1)")
    return c


def submit(c, kind, vid=1, report=1):
    c.execute(f"update {kind}_model_versions set qc_json='{{}}', qc_report_file_id={report} where id={vid}")
    c.execute(f"update {kind}_model_versions set status='qc_ready' where id={vid}")


def approve(c, kind, vid=1, report=1):
    c.execute("insert into approvals(model_kind,version_id,decision,reviewer,qc_report_file_id) values(?,?,'approved','Gugun',?)", (kind, vid, report))


def status(c, kind, vid=1):
    return c.execute(f"select status from {kind}_model_versions where id={vid}").fetchone()[0]


def blocked(c, sql, match):
    with pytest.raises(sqlite3.DatabaseError, match=match):
        c.execute(sql)


def test_autonumber(con):
    con.execute("insert into fluid_model_versions(fluid_model_id,unit_system,phases) values(1,'METRIC','OIL')")
    assert [r[0] for r in con.execute("select version from fluid_model_versions order by id")] == [1, 2]


def test_insert_must_be_draft(con):
    blocked(con, "insert into fluid_model_versions(fluid_model_id,unit_system,phases,status) values(1,'METRIC','OIL','approved')", "must start as draft")


def test_qc_ready_needs_report(con):
    blocked(con, "update fluid_model_versions set status='qc_ready' where id=1", "QC report")


def test_frozen_while_qc_ready(con):
    con.execute("insert into pvto values(1,1,50,100,1.2,1.1,1)")
    submit(con, "fluid")
    blocked(con, "insert into pvto values(1,1,60,120,1.25,1.0,1)", "frozen")
    blocked(con, "update fluid_model_versions set qc_json='x' where id=1", "frozen for review")


def test_agent_cannot_self_approve(con):
    submit(con, "fluid")
    blocked(con, "update fluid_model_versions set status='approved' where id=1", "approvals table")


def test_approval_must_match_reviewed_report(con):
    submit(con, "fluid")
    blocked(con, "insert into approvals(model_kind,version_id,decision,reviewer,qc_report_file_id) values('fluid',1,'approved','Gugun',2)", "does not match")


def test_reject_needs_comment(con):
    submit(con, "fluid")
    blocked(con, "insert into approvals(model_kind,version_id,decision,reviewer,qc_report_file_id) values('fluid',1,'rejected','Gugun',1)", "CHECK")


def test_approved_is_immutable_and_log_append_only(con):
    con.execute("insert into pvto values(1,1,50,100,1.2,1.1,1)")
    submit(con, "fluid"); approve(con, "fluid")
    assert status(con, "fluid") == "approved"
    blocked(con, "update pvto set bo=1.3 where version_id=1", "frozen")
    blocked(con, "delete from fluid_model_versions where id=1", "only draft")
    blocked(con, "delete from approvals", "append-only")


def _approve_all(con):
    for k in ("fluid", "geo", "well"):
        submit(con, k); approve(con, k)


DECK = "insert into decks(name,requirement_text,spec_json,fluid_version_id,geo_version_id,well_version_id{extra}) values('d','r','{{}}',1,1,1{val})"


def test_deck_requires_approved(con):
    blocked(con, DECK.format(extra="", val=""), "not approved")
    _approve_all(con)
    con.execute(DECK.format(extra="", val=""))
    blocked(con, "update decks set fluid_version_id=1", "cannot be changed")


def test_demo_isolation(con):
    _approve_all(con)
    blocked(con, DECK.format(extra=",origin", val=",'demo'"), "demo and non-demo")


def test_revise_path(con):
    submit(con, "fluid")
    con.execute("update fluid_model_versions set status='draft' where id=1")
    assert con.execute("select qc_ready_at from fluid_model_versions where id=1").fetchone()[0] is None
    con.execute("insert into pvto values(1,1,50,100,1.2,1.1,1)")
    con.execute("delete from fluid_model_versions where id=1")


def test_ijk_needs_geo(con):
    con.execute("insert into well_model_versions(well_model_id,unit_system) values(1,'METRIC')")
    con.execute("insert into wells(version_id,well_name,well_type,preferred_phase,head_i,head_j) values(2,'P1','PRODUCER','OIL',1,1)")
    blocked(con, "insert into well_completions(version_id,well_name,event_date,i,j,k1,k2) values(2,'P1','2026-01-01',1,1,1,3)", "geo_version_id")


def test_audit_append_only(con):
    con.execute("insert into audit_log(tool,profile,args_sha256,outcome) values('t','fluid','x','ok')")
    blocked(con, "delete from audit_log", "append-only")
