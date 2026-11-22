"""Initialisation view of a deck: the dynamic model (fluid + geo + res + well) at time zero, for ResInsight.

The deck is copied with its SUMMARY replaced by the fluids-in-place vectors and its SCHEDULE by one tiny time step
without wells, and run in the release OPM Flow. Report step 0 of the restart file is the equilibrated initial state
(PRESSURE, SWAT, SGAS, RS / RV) and the summary at time 0 holds the fluids in place, for the field and per FIPNUM
region. Nothing flows: no well is opened and the one step is 1e-4 day. The view never changes the deck or its runs.

    <file_store>/init_views/<deck>_deck<id>/INIT_<DECK>.DATA  (+ .EGRID .INIT .UNRST .SMSPEC, run.log, init_view.json)

Checks, when the deck's res model has contacts: cells clearly below the OWC hold water only, and cells clearly below
the GOC hold no free gas.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..sim.runner import prepare

FIELD_VECTORS = ["FOIP", "FGIP", "FWIP", "FPR"]
REGION_VECTORS = ["ROIP", "RGIP", "RWIP", "RPR"]
STEP_DAYS = 1e-4
SW_TOL = 0.01         # below the OWC: water saturation within this of the table's maximum
SGAS_NONE = 1e-3      # below the GOC: no more free gas than this


def _section_index(lines: list[str], name: str) -> int | None:
    pat = re.compile(rf"^\s*{name}\s*(--.*)?$")
    return next((i for i, ln in enumerate(lines) if pat.match(ln)), None)


def init_deck_text(text: str) -> str:
    """The deck up to its SUMMARY (or SCHEDULE) section, then our SUMMARY and a one-step SCHEDULE without wells."""
    lines = text.splitlines()
    i_sum, i_sch = _section_index(lines, "SUMMARY"), _section_index(lines, "SCHEDULE")
    if i_sch is None:
        raise ToolError(INVALID_INPUT, "the deck has no SCHEDULE section")
    head = lines[:i_sum if i_sum is not None and i_sum < i_sch else i_sch]
    i_grid = _section_index(head, "GRID")
    if i_grid is not None and _section_index(head, "INIT") is None:   # INIT holds DEPTH, PORV, the regions
        head = head[:i_grid + 1] + ["INIT"] + head[i_grid + 1:]
    tail = ["-- Initialisation view (opmagent): the deck at time zero. SUMMARY and SCHEDULE replaced:",
            "-- fluids in place, no wells, one 1e-4 day step so Flow writes the initial state. Not for simulation.",
            "SUMMARY", *FIELD_VECTORS, *[x for v in REGION_VECTORS for x in (v, "/")],
            "SCHEDULE", "RPTRST", " 'BASIC=2' /", "TSTEP", f" {STEP_DAYS:g} /", "END"]
    return "\n".join(head + tail) + "\n"


def _run(deck_path: Path, out: Path, flow_bin: str, timeout: int) -> Path:
    exe = shutil.which(flow_bin)
    if not exe:
        raise ToolError(INVALID_INPUT, f"the initialisation view needs the OPM Flow binary ([paths] flow_bin = "
                                       f"{flow_bin} is not on PATH)")
    if out.exists():
        shutil.rmtree(out)
    copy = prepare(deck_path, out)                  # the deck copied, its sibling include files linked
    name = re.sub(r"[^A-Za-z0-9_]+", "_", f"INIT_{deck_path.stem}").upper()
    deck = out / f"{name}.DATA"
    deck.write_text(init_deck_text(copy.read_text(errors="replace")))
    copy.unlink()
    log = out / "run.log"
    with open(log, "w") as fh:
        r = subprocess.run([exe, str(deck), f"--output-dir={out}"], stdout=fh, stderr=subprocess.STDOUT, cwd=out,
                           timeout=timeout)
    if r.returncode != 0 or not (out / f"{name}.UNRST").is_file():
        tail = log.read_text(errors="replace").strip().splitlines()[-8:]
        raise ToolError("INIT_VIEW_FAILED", "OPM Flow could not initialise the deck: " + " | ".join(tail),
                        {"deck": str(deck), "log": str(log)})
    return deck


def initial_state(folder: Path) -> dict:
    """Report step 0 and the fluids in place at time 0 of an initialisation (or any) run folder."""
    from opm.io.ecl import EclFile, ERst, ESmry
    folder = Path(folder)
    stem = next(folder.glob("*.UNRST")).stem
    rst = ERst(str(folder / f"{stem}.UNRST"))
    step = rst.report_steps[0]
    names = [a[0] for a in rst.arrays(step)]
    state = {k: np.asarray(rst[k, step], dtype=float) for k in ("PRESSURE", "SWAT", "SGAS", "RS", "RV") if k in names}
    init = EclFile(str(folder / f"{stem}.INIT"))
    # PORV covers every cell; DEPTH, DZ and the region arrays only the active ones, as the restart arrays do
    static = {a[0]: np.asarray(init[a[0]], dtype=float) for a in init.arrays
              if a[0] in ("PORV", "DEPTH", "DZ", "EQLNUM", "FIPNUM", "SATNUM")}
    smry = ESmry(str(folder / f"{stem}.SMSPEC"))
    keys = set(smry.keys())
    in_place = {k: float(smry[k][0]) for k in FIELD_VECTORS if k in keys}
    units = {}
    for k in FIELD_VECTORS:
        try:
            units[k] = smry.units(k)
        except Exception:  # noqa: BLE001
            pass
    regions: dict[int, dict] = {}
    for k in sorted(keys):
        m = re.match(r"^(ROIP|RGIP|RWIP|RPR):(\d+)$", k)
        if m:
            regions.setdefault(int(m.group(2)), {})[m.group(1)] = float(smry[k][0])
    return {"stem": stem, "state": state, "static": static, "in_place": in_place, "units": units, "regions": regions}


def _table_ends(swof: list[dict]) -> dict[int, tuple[float, float]]:
    """Per SATNUM: the SWOF table's maximum water saturation and its capillary pressure there."""
    out = {}
    for r in swof:
        s = int(r.get("satnum", 1))
        if s not in out or r["sw"] > out[s][0]:
            out[s] = (float(r["sw"]), float(r.get("pcow") or 0.0))
    return out


def checks(s: dict, contacts: list[dict], swof: list[dict] | None = None) -> list[dict]:
    """The initial state against the contacts (per EQLNUM): cells clearly below the OWC at the saturation table's
    maximum water saturation, and no free gas in cells clearly below the GOC. Below the OWC the check is n/a for
    tables whose capillary pressure at maximum Sw is not zero (a transition zone reaches below the contact)."""
    ends = _table_ends(swof or [])
    out = []
    st, sta = s["state"], s["static"]
    porv = sta.get("PORV")
    if porv is None or "DEPTH" not in sta:
        return out
    active = porv > 0
    n = int(active.sum())
    depth = sta["DEPTH"][active] if sta["DEPTH"].size == porv.size else sta["DEPTH"]
    half = (sta["DZ"][active] if "DZ" in sta and sta["DZ"].size == porv.size else np.zeros(n)) / 2
    eql = (sta["EQLNUM"][active] if "EQLNUM" in sta and sta["EQLNUM"].size == porv.size else
           sta["EQLNUM"] if "EQLNUM" in sta else np.ones(n)).astype(int)
    sat = (sta["SATNUM"] if "SATNUM" in sta and sta["SATNUM"].size == n else np.ones(n)).astype(int)
    swmax = np.array([ends.get(k, (1.0, 0.0))[0] for k in sat]) if ends else np.ones(n)
    pc_end = np.array([ends.get(k, (1.0, 0.0))[1] for k in sat]) if ends else np.zeros(n)
    for c in contacts:
        sel = eql == int(c.get("eqlnum", 1))
        e = c.get("eqlnum", 1)
        if c.get("owc_depth") is not None and "SWAT" in st:
            below = sel & (depth - half > c["owc_depth"])
            with_pc = below & (np.abs(pc_end) > 1e-9)
            judged = below & ~with_pc
            badm = judged & (st["SWAT"] < swmax - SW_TOL)
            bad = int(badm.sum())
            if judged.sum() == 0 and with_pc.sum():
                out.append({"check": f"water below the OWC (EQLNUM {e})", "status": "n/a",
                            "detail": f"{int(below.sum())} cells below {c['owc_depth']:g}; their SWOF capillary "
                                      "pressure at maximum Sw is not zero, so a transition zone reaches below the contact"})
            else:
                deep = float((depth[badm] - c["owc_depth"]).max()) if bad else 0.0
                out.append({"check": f"water below the OWC (EQLNUM {e})", "status": "pass" if bad == 0 else "warn",
                            "detail": f"{int(judged.sum())} cells below {c['owc_depth']:g}" +
                                      (f"; {bad} below the table's maximum Sw, the deepest {deep:.1f} below the contact "
                                       "(a capillary transition or end-point scaling can explain cells near it)"
                                       if bad else ", all at the table's maximum Sw")})
        if c.get("goc_depth") is not None and "SGAS" in st:
            below = sel & (depth - half > c["goc_depth"])
            badm = below & (st["SGAS"] > SGAS_NONE)
            bad = int(badm.sum())
            deep = float((depth[badm] - c["goc_depth"]).max()) if bad else 0.0
            out.append({"check": f"no free gas below the GOC (EQLNUM {e})", "status": "pass" if bad == 0 else "warn",
                        "detail": f"{int(below.sum())} cells below {c['goc_depth']:g}" +
                                  (f"; {bad} hold free gas, the deepest {deep:.1f} below the contact (a capillary "
                                   "transition can explain cells near it)" if bad else ", none hold free gas")})
    return out


def _deck_row(con, deck_id: int) -> dict:
    r = con.execute("SELECT d.*, f.path FROM decks d LEFT JOIN files f ON f.id = d.deck_file_id WHERE d.id = ?",
                    (deck_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"deck {deck_id} not found")
    if not r["path"] or not Path(r["path"]).is_file():
        raise ToolError(NOT_FOUND, f"deck {deck_id} has no deck file on disk")
    return dict(r)


def folder_of(cfg, con, deck_id: int) -> Path:
    d = _deck_row(con, deck_id)
    # outside the deck's folder: the init run links the deck's sibling files into its own folder
    return cfg.file_store / "init_views" / f"{d['name']}_deck{deck_id}"


def build_init_view(cfg, con, deck_id: int, *, timeout: int = 1800) -> dict:
    """Initialise a built deck in OPM Flow and report its initial state. Writes files in the deck's folder only."""
    d = _deck_row(con, deck_id)
    out = folder_of(cfg, con, deck_id)
    deck = _run(Path(d["path"]), out, cfg.flow_bin, timeout)
    s = initial_state(out)
    contacts = [dict(r) for r in con.execute("SELECT * FROM contacts WHERE version_id = ? ORDER BY eqlnum",
                                             (d["res_version_id"],))] if d.get("res_version_id") else []
    swof = [dict(r) for r in con.execute("SELECT * FROM swof WHERE version_id = ?", (d["res_version_id"],))] \
        if d.get("res_version_id") else []
    found = checks(s, contacts, swof)
    st, porv = s["state"], s["static"].get("PORV")
    act = porv > 0 if porv is not None else None
    hc = None
    if act is not None and "SWAT" in st and st["SWAT"].size == int(act.sum()):
        hc = float((porv[act] * (1 - st["SWAT"])).sum() / porv[act].sum())
    summary = {"deck_id": deck_id, "deck": d["name"], "res_version_id": d.get("res_version_id"),
               "in_place": s["in_place"], "units": s["units"], "regions": s["regions"],
               "pressure": [float(st["PRESSURE"].min()), float(st["PRESSURE"].max())] if "PRESSURE" in st else None,
               "hydrocarbon_pv_fraction": hc, "checks": found}
    (out / "init_view.json").write_text(json.dumps(summary, indent=1))
    ip, u = s["in_place"], s["units"]
    warn = [c for c in found if c["status"] == "warn"]
    say = (f"Deck {d['name']} initialises: oil in place {ip.get('FOIP', 0):,.0f} {u.get('FOIP', '')}, gas "
           f"{ip.get('FGIP', 0):,.0f} {u.get('FGIP', '')}, water {ip.get('FWIP', 0):,.0f} {u.get('FWIP', '')}, "
           f"average pressure {ip.get('FPR', 0):,.1f} {u.get('FPR', '')}" +
           (f", {len(s['regions'])} fluid-in-place regions" if len(s["regions"]) > 1 else "") + ". " +
           (f"{len(warn)} check(s) to look at: " + "; ".join(c['check'] for c in warn) + ". " if warn else "") +
           f"Open it in ResInsight with: opmagent results open --deck {deck_id} --init.")
    return {"ok": True, **summary, "folder": str(out), "init_deck": str(deck),
            "egrid": str(out / f"{s['stem']}.EGRID"), "spoken_summary": say}


def init_case(cfg, con, deck_id: int) -> Path | None:
    """The EGRID of a deck's initialisation view, if it has been made."""
    f = folder_of(cfg, con, deck_id)
    return next(iter(sorted(f.glob("*.EGRID"))), None) if f.is_dir() else None


def after_build(cfg, con, deck_id: int) -> dict:
    """Make the initialisation view right after a deck build; never fails the build."""
    try:
        v = build_init_view(cfg, con, deck_id)
        return {"init_view": {k: v[k] for k in ("folder", "in_place", "units", "checks", "hydrocarbon_pv_fraction")},
                "init_view_say": " " + v["spoken_summary"]}
    except Exception as e:  # noqa: BLE001
        return {"init_view_error": getattr(e, "message", None) or f"{type(e).__name__}: {e}", "init_view_say": ""}
