from __future__ import annotations

import re
from pathlib import Path


class Issues(list):
    def error(self, rule: str, message: str, where: str | None = None):
        self.append({"rule": rule, "severity": "error", "message": message, **({"where": where} if where else {})})

    def warn(self, rule: str, message: str, where: str | None = None):
        self.append({"rule": rule, "severity": "warning", "message": message, **({"where": where} if where else {})})


def rows(con, sql: str, *args) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def strictly_increasing(xs) -> bool:
    return all(b > a for a, b in zip(xs, xs[1:]))


def non_decreasing(xs, tol=0.0) -> bool:
    return all(b >= a - tol for a, b in zip(xs, xs[1:]))


def non_increasing(xs, tol=0.0) -> bool:
    return all(b <= a + tol for a, b in zip(xs, xs[1:]))


def contiguous_regions(values) -> bool:
    s = sorted(set(values))
    return bool(s) and s == list(range(1, len(s) + 1))


_REPEAT = re.compile(r"^(\d+)\*(.*)$")


def read_array(path: str | Path) -> list[float]:
    """Read a cell array from a GRDECL-style or plain whitespace file.

    Skips '--' comments, keyword lines (alphabetic first token) and the '/' terminator;
    expands Eclipse repeat syntax 'N*value'.
    """
    out: list[float] = []
    for line in Path(path).read_text().splitlines():
        line = line.split("--", 1)[0].strip()
        if not line:
            continue
        for tok in line.split():
            if tok == "/":
                return out
            if tok[0].isalpha():
                break  # keyword line
            m = _REPEAT.match(tok)
            if m:
                out.extend([float(m.group(2))] * int(m.group(1)))
            else:
                out.append(float(tok))
    return out
