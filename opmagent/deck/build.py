"""Deck builder: spec + approved models -> rendered deck -> OPM parse checks -> registered deck."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from ..core.errors import INVALID_INPUT, ToolError
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from .render import RenderError, combine, render


def _ok(r: dict) -> dict:
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def _pick(svc: DataService, kind: str, ref) -> int:
    """ref: version id (int) or a model name ('latest approved' of that model)."""
    if isinstance(ref, int):
        return ref
    rows = [m for m in svc.list_models(kind=kind)["models"] if m["model_name"] == ref]
    if not rows:
        raise ToolError(INVALID_INPUT, f"no approved {kind} model named '{ref}'")
    return max(rows, key=lambda m: m["version"])["version_id"]


def cross_checks(fluid: dict, geo: dict, well: dict) -> list[str]:
    """Checks no single modeler can make. Returns a list of problems."""
    probs = []
    ft = fluid["tables"]
    p_max = max([r["pressure"] for r in ft["pvto"] + ft["pvdo"] + ft["pvdg"]] or [0])
    p_min = min([r["pressure"] for r in ft["pvto"] + ft["pvdo"] + ft["pvdg"]] or [0])
    for c in geo["tables"]["contacts"]:
        if not p_min <= c["datum_pressure"] <= p_max:
            probs.append(f"initial pressure {c['datum_pressure']:g} bar is outside the PVT range {p_min:g}-{p_max:g} bar")
    for e in well["tables"]["well_events"]:
        if e["bhp"] is not None and not p_min <= e["bhp"] <= p_max:
            probs.append(f"{e['well_name']} BHP {e['bhp']:g} bar is outside the PVT range {p_min:g}-{p_max:g} bar")
    nsat = max([1] + [r["satnum"] for r in geo["tables"]["swof"]])
    npvt = max([1] + [r["pvtnum"] for r in ft["pvto"] + ft["pvdg"]])
    for kw, n in (("SATNUM", nsat), ("PVTNUM", npvt)):
        p = next((p for p in geo["tables"]["geo_properties"] if p["keyword"] == kw and p["const_value"] is not None), None)
        if p and int(p["const_value"]) > n:
            probs.append(f"{kw} uses region {int(p['const_value'])} but only {n} table set(s) exist")
    wv = well["header"].get("geo_version_id")
    if wv and wv != geo["header"]["id"]:
        probs.append("well model was built for a different geo version")
    return probs


_PARSE = r"""
import json, sys
from opm.io.parser import Parser, ParseContext
from opm.io.ecl_state import EclipseState
from opm.io.schedule import Schedule
from opm.io.summary import SummaryConfig
deck = Parser().parse(sys.argv[1], ParseContext())
st = EclipseState(deck)
sch = Schedule(deck, st)
SummaryConfig(deck, st, sch)
g = st.grid()
print(json.dumps({"active_cells": g.nactive, "wells": [w.name for w in sch.get_wells(len(sch.reports) - 1)] if hasattr(sch, "reports") else None}))
"""


def opm_parse_check(deck_path: Path) -> tuple[bool, str]:
    """Parse with OPM in a separate process (EclipseState, Schedule, SummaryConfig)."""
    try:
        import opm.io  # noqa: F401
    except ImportError:
        return False, "OPM Python bindings not installed (pip install opm); deck not parsed"
    r = subprocess.run([sys.executable, "-c", _PARSE, str(deck_path)], capture_output=True, text=True, timeout=300)
    log = (r.stdout + r.stderr).strip()
    return r.returncode == 0, log[-4000:]


def build_deck(spec: dict, cfg, *, origin: str | None = None) -> dict:
    """spec: name, requirement, fluid, geo, res, well (version id or model name), start_date, end_date|years,
    report, summary."""
    svc = DataService("deck", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    name = spec.get("name")
    if not name or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ToolError(INVALID_INPUT, "spec.name must be letters, digits, _ or -")
    if not spec.get("res"):
        raise ToolError(INVALID_INPUT, "spec.res is missing: a deck needs the res model (SCAL, rock, contacts, regions) "
                                       "built on its geo version")
    ids = {k: _pick(svc, k, spec[k]) for k in ("fluid", "geo", "res", "well")}
    fluid, geo_only, res, well = (D("get_model", kind=k, version_id=ids[k]) for k in ("fluid", "geo", "res", "well"))
    try:
        geo = combine(geo_only, res)
    except RenderError as e:
        raise ToolError("CROSS_CHECK_FAILED", str(e)) from e
    origins = {m["header"]["origin"] for m in (fluid, geo_only, res, well)}
    origin = origin or ("demo" if origins == {"demo"} else "built")
    spec = dict(spec)
    spec.setdefault("start_date", well["header"].get("start_date") or "2026-01-01")
    if "end_date" not in spec:
        s = date.fromisoformat(spec["start_date"])
        spec["end_date"] = s.replace(year=s.year + int(spec.get("years", 1))).isoformat()
    probs = cross_checks(fluid, geo, well)
    # imported, or rebuilt from a deck's data (a study's reverse process): the data ran in OPM as it is
    imported = any(m["header"]["origin"] == "imported" or m["header"].get("source_deck_file_id")
                   for m in (fluid, geo_only, res, well))
    if probs and not imported:
        raise ToolError("CROSS_CHECK_FAILED", "; ".join(probs), probs)

    file_ids = [p["file_id"] for p in geo["tables"]["geo_properties"] if p["file_id"]]
    if geo["header"].get("grid_file_id"):
        file_ids.append(geo["header"]["grid_file_id"])
    paths = {fid: svc.con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()["path"] for fid in file_ids}
    try:
        text = render(spec, fluid, geo, well, paths)
    except RenderError as e:
        raise ToolError("RENDER_FAILED", str(e)) from e

    out = cfg.file_store / "decks" / name
    out.mkdir(parents=True, exist_ok=True)
    deck_path = out / f"{name.upper()}.DATA"
    deck_path.write_text(text)
    deck = D("create_deck", name=name, requirement_text=spec.get("requirement", ""), spec=spec,
             fluid_version_id=ids["fluid"], geo_version_id=ids["geo"], well_version_id=ids["well"],
             res_version_id=ids["res"], origin=origin)["deck"]
    D("add_deck_file", deck_id=deck["id"], path=str(deck_path), role="DATA", main=True)
    ok, log = opm_parse_check(deck_path)
    status = "validated" if ok else "failed"
    D("set_deck_status", deck_id=deck["id"], status=status, validation_log=log)
    say = (f"Deck {name} is built and passed the OPM parser." if ok else
           f"Deck {name} was written but failed the OPM check: {log.splitlines()[-1] if log else 'no output'}")
    return {"ok": ok, "deck_id": deck["id"], "path": str(deck_path), "status": status, "validation_log": log,
            "cross_check_warnings": probs if imported else [],
            "models": {k: f"{m['header']['model_name']} v{m['header']['version']}" for k, m in
                       (("fluid", fluid), ("geo", geo_only), ("res", res), ("well", well))}, "spoken_summary": say}
