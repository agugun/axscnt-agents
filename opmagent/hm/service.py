"""History-match functions on the database: observations for a study or a run, the misfit of a finished run, and
the HM report. Shared by the HM server (opmagent-hm) and `opmagent hm`. Read only: they write nothing but the
report folder under <file_store>/hm/.

Observation sources:
  data_production   the measured history the data manager loaded for the study (volumes -> monthly rates)
  deck_history      what the deck gave Flow under history control (WOPRH, WBHPH ...), read from the run itself;
                    for a study from a deck, which has no data_production
  run               another run's simulated series, as a synthetic truth (tests, phase 2's known answer)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..core.errors import INVALID_INPUT, NO_HISTORY, NOT_FOUND, ToolError
from ..data.store import study_id
from . import misfit as mf
from . import series

SOURCES = ("auto", "data_production", "deck_history", "run")
REPORT_TEMPLATE = Path(__file__).resolve().parent.parent / "notebooks" / "hm_report.ipynb"


def run_row(con, run_id: int) -> dict:
    r = con.execute("SELECT r.*, d.name AS deck_name, d.well_version_id FROM runs r JOIN decks d ON d.id = r.deck_id "
                    "WHERE r.id = ?", (run_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"run {run_id} not found")
    r = dict(r)
    if r["status"] not in ("done", "stored") or not r["smspec_path"] or not Path(r["smspec_path"]).exists():
        raise ToolError(INVALID_INPUT, f"run {run_id} is {r['status']}; the misfit needs a finished run")
    return r


def _production(con, study) -> pd.DataFrame:
    sid = study_id(con, study)
    return pd.read_sql_query("SELECT well_name, date, oil, water, gas, water_inj, gas_inj, bhp, thp, hours "
                             "FROM data_production WHERE study_id = ? ORDER BY well_name, date", con, params=(sid,))


def observations(con, *, study: str | None = None, run_id: int | None = None, source: str = "auto",
                 truth_run_id: int | None = None, wells: list[str] | None = None) -> tuple[str, pd.DataFrame]:
    """(source used, monthly observations). auto: data_production if the study has any, else the run's deck
    history."""
    if source not in SOURCES:
        raise ToolError(INVALID_INPUT, f"source must be one of {', '.join(SOURCES)}")
    if source == "run":
        if truth_run_id is None:
            raise ToolError(INVALID_INPUT, "source 'run' needs truth_run_id (the run whose series are the truth)")
        return "run", series.from_summary(run_row(con, truth_run_id)["smspec_path"], wells=wells)
    if source in ("auto", "data_production") and study:
        obs = series.from_production(_production(con, study), wells)
        if not obs.empty or source == "data_production":
            if obs.empty:
                raise ToolError(NO_HISTORY, f"study {study} has no measured production history")
            return "data_production", obs
    if source == "data_production":
        raise ToolError(INVALID_INPUT, "source 'data_production' needs a study")
    if run_id is None:
        raise ToolError(NO_HISTORY, "no observations: give a study with production history, or a run whose deck "
                                    "has history (WCONHIST / WCONINJH)")
    # Flow also writes the prediction targets into WOPRH / WWIRH ..., so only wells the deck puts under history
    # control (HISTORY events in its well model) count
    r = run_row(con, run_id)
    hist = {x["well_name"] for x in con.execute("SELECT DISTINCT well_name FROM well_events WHERE version_id = ? "
                                                 "AND mode = 'HISTORY'", (r["well_version_id"],))}
    if wells:
        hist &= set(wells)
    obs = series.from_summary(r["smspec_path"], use_history=True, wells=sorted(hist)) if hist else series.empty()
    if obs.empty:
        where = f"study {study} has no production history and " if study else ""
        raise ToolError(NO_HISTORY, f"{where}run {run_id}'s deck gave Flow no history (no WOPRH / WBHPH ...)")
    return "deck_history", obs


def observation_inventory(con, *, study: str | None = None, run_id: int | None = None, source: str = "auto",
                          truth_run_id: int | None = None, wells: list[str] | None = None) -> dict:
    """What can be matched: per well the quantities with data, their points and months, the history control and
    so the quantities Flow is given (honoured, not matched)."""
    src, obs = observations(con, study=study, run_id=run_id, source=source, truth_run_id=truth_run_id, wells=wells)
    hon = mf.honoured(con, run_row(con, run_id)["well_version_id"]) if run_id is not None else {}
    out = []
    for w, g in obs.groupby("well"):
        qs = {}
        for q, gq in g.groupby("quantity"):
            nz = int((gq["value"].abs() > mf.ZERO).sum())
            if nz == 0 and q != "bhp":
                continue
            qs[q] = {"points": int(len(gq)), "non_zero": nz, "first": gq["month"].min(), "last": gq["month"].max(),
                     "zero_day_months": int((gq["days"] <= 0).sum())}
        h = hon.get(w, {"controls": [], "honoured": []})
        mask = mf.honoured_mask(g, hon)
        months_hon = {q: int(mask[(g["quantity"] == q).to_numpy()].sum()) for q in qs}
        out.append({"well": w, "quantities": qs, "controls": h["controls"], "honoured": h["honoured"],
                    "honoured_months": {q: n for q, n in months_hon.items() if n},
                    "matched": sorted(q for q in qs if months_hon[q] < qs[q]["points"])})
    first, last = (obs["month"].min(), obs["month"].max()) if not obs.empty else (None, None)
    return {"source": src, "period": [first, last], "wells": out, "points": int(len(obs))}


def run_misfit(con, run_id: int, *, study: str | None = None, source: str = "auto", truth_run_id: int | None = None,
               objective: dict | None = None) -> dict:
    """Misfit of a finished run against the observations."""
    r = run_row(con, run_id)
    try:
        obj = mf.objective(objective)
    except ValueError as e:
        raise ToolError(INVALID_INPUT, str(e)) from None
    src, obs = observations(con, study=study, run_id=run_id, source=source, truth_run_id=truth_run_id)
    sim = series.from_summary(r["smspec_path"])
    hon = mf.honoured(con, r["well_version_id"])
    res = mf.compute(obs, sim, obj, hon)
    res.update(run_id=run_id, deck_id=r["deck_id"], deck_name=r["deck_name"], source=src, objective=obj,
               observations=obs, simulated=sim, findings=mf.describe(res))
    return res


def public(res: dict, max_terms: int = 20) -> dict:
    """A misfit result as a tool returns it (no DataFrames)."""
    total = res["total"]
    words = "; ".join(res["findings"][:3])
    spoken = (f"Run {res['run_id']} against {res['source'].replace('_', ' ')}: misfit "
              + (f"{total:.2f} over {res['n']} points" if total is not None else "not defined (nothing to compare)")
              + (f". {words}." if words else "."))
    return {"ok": True, "run_id": res["run_id"], "deck_id": res["deck_id"], "source": res["source"],
            "misfit": total, "points": res["n"], "wells": res["wells"], "terms": res["terms"][:max_terms],
            "findings": res["findings"], "objective": res["objective"], "spoken_summary": spoken}


def report_dir(file_store: Path, run_id: int) -> Path:
    return Path(file_store) / "hm" / f"report_run{run_id}_{datetime.now():%Y%m%d_%H%M%S}"


def write_report(cfg, con, run_id: int, *, study: str | None = None, source: str = "auto",
                 truth_run_id: int | None = None, objective: dict | None = None, timeout: int = 600) -> dict:
    """The HM report for a run: payload (observed, simulated, misfit) and the executed notebook, as HTML."""
    from ..notebooks import runner
    res = run_misfit(con, run_id, study=study, source=source, truth_run_id=truth_run_id, objective=objective)
    folder = report_dir(cfg.file_store, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    res["observations"].to_csv(folder / "observed.csv", index=False)
    res["simulated"].to_csv(folder / "simulated.csv", index=False)
    pos = _well_positions(con, res["deck_id"])
    pub = public(res, max_terms=10_000)
    (folder / "payload.json").write_text(json.dumps({
        "schema": 1, "created_at": f"{datetime.now():%Y-%m-%d %H:%M}", "study": study,
        **{k: pub[k] for k in ("run_id", "deck_id", "source", "misfit", "points", "wells", "terms", "findings",
                               "objective")},
        "deck_name": res["deck_name"], "well_positions": pos}, indent=2, default=str))
    out = runner.execute(REPORT_TEMPLATE, folder / "hm_report.ipynb", {"payload_path": "payload.json"},
                         cwd=folder, timeout=timeout)
    return {**pub, "report": out["html"], "notebook": out["notebook"], "report_ok": out["ok"],
            "report_error": out["error"], "folder": str(folder),
            "spoken_summary": pub["spoken_summary"] + (" Report written." if out["ok"]
                                                       else f" The report failed: {out['error']}")}


def _well_positions(con, deck_id: int) -> dict:
    """Well i, j for the misfit map (from the deck's well model)."""
    d = con.execute("SELECT well_version_id FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if not d:
        return {}
    try:
        rows = con.execute("SELECT well_name, head_i, head_j FROM wells WHERE version_id = ?",
                           (d["well_version_id"],)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {r["well_name"]: [r["head_i"], r["head_j"]] for r in rows if r["head_i"] is not None}
