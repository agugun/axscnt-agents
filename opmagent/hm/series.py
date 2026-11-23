"""Monthly series: the common shape of observed and simulated history.

A series is a DataFrame with columns well, quantity, month (first day of the month, ISO), value, days.
`value` is a monthly average rate (or the mean BHP), `days` the producing days the rate is over.

Observed rates are monthly volume / producing days, as the well modeler writes them into HISTORY events
(`opmagent.well.ingest.monthly_history`). Simulated rates are the time-weighted mean of the summary rate over the
calendar month: under history control without WEFAC Flow produces the given rate for the whole month, so the two
are the same number when the well honours it.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# quantity -> (simulated vector, observed-in-deck vector, data_production column)
QUANTITIES = {
    "oil": ("WOPR", "WOPRH", "oil"),
    "water": ("WWPR", "WWPRH", "water"),
    "gas": ("WGPR", "WGPRH", "gas"),
    "water_inj": ("WWIR", "WWIRH", "water_inj"),
    "gas_inj": ("WGIR", "WGIRH", "gas_inj"),
    "bhp": ("WBHP", "WBHPH", "bhp"),
}
RATES = ("oil", "water", "gas", "water_inj", "gas_inj")
COLUMNS = ["well", "quantity", "month", "value", "days"]


def empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def _month_starts(t0: datetime, t1: datetime) -> list[date]:
    """First days of the calendar months fully inside [t0, t1]."""
    d = date(t0.year, t0.month, 1)
    if datetime(d.year, d.month, d.day) < t0:
        d = _next_month(d)
    out = []
    while datetime.combine(_next_month(d), datetime.min.time()) <= t1:
        out.append(d)
        d = _next_month(d)
    return out


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def monthly_mean(time_days: list[float], values: list[float], start: datetime) -> list[tuple[date, float]]:
    """Time-weighted monthly mean of a summary vector.

    Summary rates are step values: the value reported at t_i holds over (t_{i-1}, t_i]. The integral is
    piecewise linear in time, so a month's mean is (C(m1) - C(m0)) / (m1 - m0). Only months fully inside the
    simulated time are returned.
    """
    t = np.asarray([0.0] + list(time_days), dtype=float)
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return []
    cum = np.concatenate([[0.0], np.cumsum(v * np.diff(t))])

    def C(x):
        return float(np.interp(x, t, cum))

    end = start + timedelta(days=float(t[-1]))
    out = []
    for m in _month_starts(start, end):
        a = (datetime.combine(m, datetime.min.time()) - start).total_seconds() / 86400
        b = (datetime.combine(_next_month(m), datetime.min.time()) - start).total_seconds() / 86400
        out.append((m, (C(b) - C(a)) / (b - a)))
    return out


def read_summary(smspec: str | Path) -> tuple[datetime, list[float], dict[str, list[float]]]:
    """(start date, time in days, {vector: values}) from a run's SMSPEC."""
    from opm.io.ecl import ESmry
    s = ESmry(str(smspec))
    out = {}
    for k in s.keys():
        if ":" in k:
            try:
                out[k] = [float(x) for x in s[k]]
            except Exception:  # noqa: BLE001
                continue
    return s.start_date, [float(x) for x in s["TIME"]], out


def from_summary(smspec: str | Path, use_history: bool = False, wells: list[str] | None = None) -> pd.DataFrame:
    """Monthly series of a run: simulated (WOPR ...) or, with use_history, the observations the deck gave Flow
    (WOPRH ...). A history vector that is zero throughout is left out (Flow writes zeros for wells with no
    history)."""
    start, t, vec = read_summary(smspec)
    rows = []
    for q, (sim, hist, _) in QUANTITIES.items():
        kw = hist if use_history else sim
        for key, vals in vec.items():
            name, well = key.split(":", 1)
            if name != kw or (wells and well not in wells):
                continue
            if use_history and not any(abs(x) > 0 for x in vals):
                continue
            for m, val in monthly_mean(t, vals, start):
                rows.append((well, q, m.isoformat(), val, float(_days_in_month(m))))
    return pd.DataFrame(rows, columns=COLUMNS) if rows else empty()


def _days_in_month(m: date) -> int:
    return (_next_month(m) - m).days


def from_production(df: pd.DataFrame, wells: list[str] | None = None) -> pd.DataFrame:
    """Monthly series from data_production rows (daily or monthly volumes, BHP, hours), aggregated the way the
    well modeler aggregates them: rate = volume / producing days (hours / 24 for daily data, the calendar month
    for monthly data); BHP the mean of the non-zero values."""
    if df is None or df.empty:
        return empty()
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M")
    for c in ("oil", "water", "gas", "water_inj", "gas_inj", "hours", "bhp"):
        if c not in df:
            df[c] = np.nan
    daily = df["date"].dt.day.nunique() > 1
    rows = []
    for (w, m), g in df.groupby(["well_name", "month"]):
        w = str(w)
        if wells and w not in wells:
            continue
        days = (g["hours"].fillna(24).clip(0, 24).sum() / 24.0) if daily else float(m.days_in_month)
        mo = m.start_time.date().isoformat()
        for q in RATES:
            if g[q].notna().any():
                vol = float(g[q].fillna(0).sum())
                rows.append((w, q, mo, vol / days if days > 0 else 0.0, days))
        if g["bhp"].notna().any():
            b = float(g["bhp"].replace(0, np.nan).mean())
            if not math.isnan(b):
                rows.append((w, "bhp", mo, b, days))
    return pd.DataFrame(rows, columns=COLUMNS) if rows else empty()
