"""Validator rules per model kind. Each returns a list of issues.

An issue is {"rule", "severity": "error"|"warning", "message", "where"?}.
Errors block submission for review; warnings are reported in the QC report.
"""
from __future__ import annotations

import sqlite3

from . import fluid, geo, res, well

_RUNNERS = {"fluid": fluid.validate, "geo": geo.validate, "res": res.validate, "well": well.validate}


def run(kind: str, con: sqlite3.Connection, version_id: int) -> list[dict]:
    return _RUNNERS[kind](con, version_id)


def split(issues: list[dict]) -> tuple[list[dict], list[dict]]:
    return [i for i in issues if i["severity"] == "error"], [i for i in issues if i["severity"] == "warning"]
