"""Deck-level comparison of two decks, without running either: is the generated deck the same simulation deck?

Both decks go through the importer (the OPM parser), so the comparison is on what the simulator reads, not on
the text (keyword order, number formatting, include layout and comments may differ):

1. grid     - dimensions and active cells
2. tables   - every fluid, geo and well table row (PVT, SCAL, rock, contacts, faults, wells, completions, controls)
3. arrays   - every cell property as OPM processes it, plus per-cell depth and volume (roundtrip.compare_static_decks)
4. schedule - start and end date, report dates, schedule mode, keywords carried verbatim, summary keywords

Numbers compare with a relative tolerance of 1e-9 (the importer reads both decks the same way, so a rebuilt deck
normally matches exactly).
"""
from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path

REL_TOL = 1e-9
# header items that name files or the deck itself, not what the simulator reads
_HEADER_SKIP = {"grid_file"}
# keywords that only control printed or written output, not what is simulated (the deck builder adds some so
# that ResInsight gets restart files): differences in them are reported, not failed
OUTPUT_ONLY = {"RPTRST", "RPTSOL", "RPTSCHED", "RPTPROPS", "RPTGRID", "RPTRUNSP", "RPTSMRY", "RPTONLY", "RPTONLYO",
               "ECHO", "NOECHO", "INIT", "GRIDFILE", "UNIFIN", "UNIFOUT", "FMTIN", "FMTOUT", "NOINSPEC", "NORSSPEC",
               "MESSAGES", "DEBUG", "NOWARN", "WARN", "EXTRAPMS"}
EXTRA_REGIONS = {"IMBNUM", "ROCKNUM", "MULTNUM", "OPERNUM", "FLUXNUM", "MISCNUM", "KRNUMX", "KRNUMY", "KRNUMZ",
                 "IMBNUMX", "IMBNUMY", "IMBNUMZ", "ENDNUM", "TRACERNUM"}
# tables whose row order across wells (or groups, faults) has no effect on the simulation: compared sorted by
# these columns, keeping each well's own order
ORDER_FREE = {"well.wells": ("well_name",), "well.well_groups": ("group_name",),
              "well.well_completions": ("well_name", "event_date"),
              "well.well_events": ("well_name", "event_date", "mode"),
              "geo.fault_multipliers": ("fault_name",)}
_SPEC_KEYS = ("start_date", "end_date", "report_dates", "schedule_mode", "schedule_extra", "equil_extra",
              "passthrough", "summary_passthrough")


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0) or a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return str(a) == str(b) if a is not None and b is not None else a == b


def _norm_text(t: str) -> str:
    return " ".join(t.split())


def _rows_diff(name: str, a: list, b: list, out: list) -> None:
    a, b = a or [], b or []
    if len(a) != len(b):
        out.append(f"{name}: {len(a)} row(s) in the original, {len(b)} in the generated deck")
        return
    for i, (ra, rb) in enumerate(zip(a, b)):
        if not _same(ra, rb):
            keys = sorted(set(ra) | set(rb))
            bad = [k for k in keys if not _same(ra.get(k), rb.get(k))]
            out.append(f"{name} row {i + 1}: " + ", ".join(f"{k} {ra.get(k)!r} vs {rb.get(k)!r}" for k in bad[:4]))
            if len(out) > 200:
                return


def compare_decks(original: str | Path, generated: str | Path) -> dict:
    """Compare two decks as the simulator reads them. Returns {passed, sections: [...], spoken_summary}."""
    from . import roundtrip
    from .importer import import_deck
    original, generated = Path(original).expanduser().resolve(), Path(generated).expanduser().resolve()
    tmp = Path(tempfile.mkdtemp(prefix="deck_compare_"))
    try:
        A = import_deck(original, tmp / "original")
        B = import_deck(generated, tmp / "generated")
        sections = []

        diffs = []
        if A["grid"] != B["grid"]:
            diffs.append(f"grid {A['grid']} vs {B['grid']}")
        sections.append({"section": "grid", "passed": not diffs, "differences": diffs, "grid": A["grid"]})

        diffs, counts = [], {}
        for kind in ("fluid", "geo", "res", "well"):
            ha = {k: v for k, v in A[kind]["header"].items() if k not in _HEADER_SKIP}
            hb = {k: v for k, v in B[kind]["header"].items() if k not in _HEADER_SKIP}
            for k in sorted(set(ha) | set(hb)):
                if not _same(ha.get(k), hb.get(k)):
                    diffs.append(f"{kind} header {k}: {ha.get(k)!r} vs {hb.get(k)!r}")
            for t in sorted(set(A[kind]["tables"]) | set(B[kind]["tables"])):
                ra, rb = A[kind]["tables"].get(t) or [], B[kind]["tables"].get(t) or []
                key = ORDER_FREE.get(f"{kind}.{t}")
                if key:      # the order between wells doesn't matter; the order within one well does (stable sort)
                    ra, rb = (sorted(x, key=lambda r: tuple(str(r.get(k)) for k in key)) for x in (ra, rb))
                if ra or rb:
                    counts[f"{kind}.{t}"] = len(ra)
                _rows_diff(f"{kind}.{t}", ra, rb, diffs)
        sections.append({"section": "tables", "passed": not diffs, "differences": diffs[:200],
                         "tables_compared": len(counts), "rows_compared": sum(counts.values()), "rows": counts})

        st = roundtrip.compare_static_decks(original, generated)
        diffs = [f"{k}: " + (f"missing in the {v['missing_in']} deck" if "missing_in" in v else
                             f"max relative difference {v['max_rel_diff']:.3g}" if "max_rel_diff" in v else
                             f"{v.get('original')} vs {v.get('rebuilt')}")
                 for k, v in st["arrays"].items() if k in st["failed"]]
        eps = _endpoint_arrays(original, generated, A, B)        # end-point scaling (SWL, SGU, ISGU, ...)
        diffs += eps["differences"]
        sections.append({"section": "arrays", "passed": st["passed"] and not eps["differences"],
                         "differences": diffs, "arrays_compared": sorted(st["arrays"]) + eps["compared"]})

        diffs, output_only = [], []
        for k in _SPEC_KEYS:
            va, vb = A["spec"].get(k), B["spec"].get(k)
            if k in ("passthrough", "summary_passthrough"):
                ta, tb = _flat_text(va), _flat_text(vb)
                oa, ob = [x for x in ta if _bare(x[0]) in OUTPUT_ONLY], [x for x in tb if _bare(x[0]) in OUTPUT_ONLY]
                if oa != ob:
                    output_only += sorted({x[0] for x in set(oa) ^ set(ob)})
                ta, tb = [x for x in ta if x not in oa], [x for x in tb if x not in ob]
                if ta != tb:
                    ka, kb = [x[0] for x in ta], [x[0] for x in tb]
                    diffs.append(f"{k}: keywords {ka[:8]} vs {kb[:8]}" if ka != kb else
                                 f"{k}: same keywords, different text in " +
                                 ", ".join(x[0] for x, y in zip(ta, tb) if x != y)[:200])
            elif not _same(va, vb):
                if k == "report_dates":
                    diffs.append(f"report_dates: {len(va or [])} vs {len(vb or [])} dates")
                else:
                    diffs.append(f"{k}: {str(va)[:80]} vs {str(vb)[:80]}")
        sections.append({"section": "schedule", "passed": not diffs, "differences": diffs,
                         "output_only_differences": output_only,
                         "report_dates": len(A["spec"].get("report_dates") or []),
                         "schedule_mode": A["spec"].get("schedule_mode")})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passed = all(s["passed"] for s in sections)
    tab = sections[1]
    if passed:
        say = (f"The decks are the same simulation deck: grid, {tab['rows_compared']} table rows in "
               f"{tab['tables_compared']} tables, {len(sections[2]['arrays_compared'])} cell arrays and the "
               f"schedule ({sections[3]['report_dates']} report dates) match.")
    else:
        bad = [s for s in sections if not s["passed"]]
        say = "The decks differ in " + ", ".join(s["section"] for s in bad) + ": " + \
              "; ".join(bad[0]["differences"][:2]) + "."
    return {"passed": passed, "original": str(original), "generated": str(generated), "sections": sections,
            "spoken_summary": say}


def _endpoint_arrays(deck_a: Path, deck_b: Path, A: dict, B: dict, tol: float = 1e-6) -> dict:
    """End-point scaling arrays and the less common region arrays, as OPM processes them (not covered by
    roundtrip.compare_static_decks)."""
    import numpy as np
    from .importer import ENDPOINT_KWS, parse
    names = sorted((ENDPOINT_KWS | EXTRA_REGIONS) & {c["keyword"] for X in (A, B) for c in X["coverage"]["keywords"]})
    if not names:
        return {"compared": [], "differences": []}
    fa, fb = parse(deck_a)[1].field_props(), parse(deck_b)[1].field_props()
    compared, diffs = [], []
    for k in names:
        vals = []
        for f in (fa, fb):
            v = None
            for getter in (f.get_double_array, f.get_int_array):
                try:
                    v = np.asarray(getter(k), dtype=float)
                    break
                except Exception:  # noqa: BLE001
                    continue
            vals.append(v)
        if vals[0] is None and vals[1] is None:
            continue
        compared.append(k)
        if (vals[0] is None) != (vals[1] is None):
            diffs.append(f"{k}: missing in the {'generated' if vals[1] is None else 'original'} deck")
            continue
        a, b = vals
        if a.shape != b.shape:
            diffs.append(f"{k}: {a.size} vs {b.size} values")
            continue
        scale = max(float(np.abs(a).max(initial=0.0)), 1e-30)
        d = float((np.abs(a - b) / scale).max(initial=0.0))
        if d > tol:
            diffs.append(f"{k}: max relative difference {d:.3g}")
    return {"compared": compared, "differences": diffs}


def _bare(key: str) -> str:
    return key.split(":", 1)[-1]


def _array_keywords() -> set[str]:
    from .importer import ENDPOINT_KWS, GRID_PROPS, REGION_PROPS
    return {*GRID_PROPS, *REGION_PROPS, *ENDPOINT_KWS, *EXTRA_REGIONS, "TOPS", "DX", "DY", "DZ", "ACTNUM", "SWATINIT", "COORD", "ZCORN"}


def _flat_text(pt) -> list[tuple[str, str]]:
    """Passthrough keywords as (keyword, normalised text), in order (by section when grouped). Keywords that only
    set cell arrays (e.g. a COPY or MULTIPLY on PERMZ) are left out: the arrays section compares their result."""
    if not pt:
        return []
    if isinstance(pt, dict):
        arrays = _array_keywords()
        return [(f"{sec}:{x['keyword']}", _norm_text(x["raw_text"])) for sec in sorted(pt) for x in pt[sec]
                if not (sec in ("GRID", "EDIT", "PROPS", "REGIONS") and x["keyword"] in arrays)]
    return [(x["keyword"], _norm_text(x["raw_text"])) for x in pt]
