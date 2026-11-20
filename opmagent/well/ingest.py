"""Well modeler inputs: wells, deviation surveys, perforations, production/injection history, controls.

Each input is a CSV or an Excel sheet with the columns below (case-insensitive). A Volve adapter maps the
public Volve daily production workbook to the history format.

wells:        well_name, type (PRODUCER/INJECTOR), phase (OIL/WATER/GAS), group, x, y, ref_depth
surveys:      well_name, md, and either (x, y, tvd) or (inc, azi) [+ optional x0, y0 for the wellhead]
perforations: well_name, date, top_md, bottom_md, state (OPEN/SHUT), skin, diameter
history:      well_name, date, oil, water, gas, water_inj, gas_inj, bhp, thp, hours   (daily or monthly volumes)
controls:     well_name, date, control (ORAT/LRAT/BHP/RATE/...), rate, bhp           (prediction)
groups:       group, parent
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

VOLVE_COLUMNS = {  # Volve 'Volve production data.xlsx', daily sheet
    "DATEPRD": "date", "NPD_WELL_BORE_NAME": "well_name", "ON_STREAM_HRS": "hours",
    "AVG_DOWNHOLE_PRESSURE": "bhp", "AVG_WHP_P": "thp", "BORE_OIL_VOL": "oil", "BORE_GAS_VOL": "gas",
    "BORE_WAT_VOL": "water", "BORE_WI_VOL": "water_inj",
}


def read_table(path: str | Path, sheet: str | None = None) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(p, sheet_name=sheet or 0)
    else:
        df = pd.read_csv(p)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _lower(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    return df


def volve_history(path: str | Path, sheet: str | None = None) -> pd.DataFrame:
    """Map the Volve daily production workbook to the history format."""
    df = read_table(path, sheet)
    missing = [c for c in ("DATEPRD", "NPD_WELL_BORE_NAME") if c not in df.columns]
    if missing:
        raise ValueError(f"not a Volve daily production sheet (missing {missing})")
    df = df.rename(columns={k: v for k, v in VOLVE_COLUMNS.items() if k in df.columns})
    return df[[c for c in VOLVE_COLUMNS.values() if c in df.columns]]


def min_curvature(md, inc_deg, azi_deg, x0=0.0, y0=0.0, z0=0.0):
    """Minimum curvature: survey stations -> x (east), y (north), tvd."""
    md, inc, azi = (np.asarray(v, dtype=float) for v in (md, inc_deg, azi_deg))
    inc, azi = np.radians(inc), np.radians(azi)
    x, y, z = [x0], [y0], [z0]
    for i in range(1, len(md)):
        dmd = md[i] - md[i - 1]
        cos_dl = math.cos(inc[i] - inc[i - 1]) - math.sin(inc[i - 1]) * math.sin(inc[i]) * (1 - math.cos(azi[i] - azi[i - 1]))
        dl = math.acos(max(-1.0, min(1.0, cos_dl)))
        rf = 1.0 if dl < 1e-9 else 2 / dl * math.tan(dl / 2)
        x.append(x[-1] + dmd / 2 * (math.sin(inc[i - 1]) * math.sin(azi[i - 1]) + math.sin(inc[i]) * math.sin(azi[i])) * rf)
        y.append(y[-1] + dmd / 2 * (math.sin(inc[i - 1]) * math.cos(azi[i - 1]) + math.sin(inc[i]) * math.cos(azi[i])) * rf)
        z.append(z[-1] + dmd / 2 * (math.cos(inc[i - 1]) + math.cos(inc[i])) * rf)
    return np.array(x), np.array(y), np.array(z)


def surveys(df: pd.DataFrame, heads: dict[str, tuple[float, float]]) -> list[dict]:
    df = _lower(df)
    out = []
    for w, g in df.groupby("well_name"):
        g = g.sort_values("md")
        if {"x", "y", "tvd"} <= set(g.columns):
            xs, ys, zs = g["x"].values, g["y"].values, g["tvd"].values
        elif {"inc", "azi"} <= set(g.columns):
            x0 = float(g["x0"].iloc[0]) if "x0" in g else heads.get(w, (0.0, 0.0))[0]
            y0 = float(g["y0"].iloc[0]) if "y0" in g else heads.get(w, (0.0, 0.0))[1]
            xs, ys, zs = min_curvature(g["md"].values, g["inc"].values, g["azi"].values, x0, y0, float(g["md"].iloc[0]))
        else:
            raise ValueError(f"survey for {w} needs (x, y, tvd) or (inc, azi)")
        for md, x, y, z in zip(g["md"].values, xs, ys, zs):
            out.append({"well_name": str(w), "md": float(md), "x": float(x), "y": float(y), "tvd": float(z)})
    return out


def monthly_history(df: pd.DataFrame, wells: dict[str, dict]) -> list[dict]:
    """Aggregate daily or monthly volumes to monthly average rates (volume / producing days).

    Producers -> HISTORY rows with oil/water/gas rates (control ORAT if oil > 0 else LRAT/GRAT).
    Injectors -> HISTORY rows with inj_rate. A month with no production -> status SHUT.
    """
    df = _lower(df)
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M")
    for c in ("oil", "water", "gas", "water_inj", "gas_inj", "hours", "bhp", "thp"):
        if c not in df:
            df[c] = np.nan
    daily = df["date"].dt.day.nunique() > 1   # several dates per month -> daily volumes
    events = []
    for (w, m), g in df.groupby(["well_name", "month"]):
        w = str(w)
        if w not in wells:
            continue
        days = (g["hours"].fillna(24).clip(0, 24).sum() / 24.0) if daily else float(m.days_in_month)
        start = m.start_time.date().isoformat()
        inj = wells[w]["well_type"] == "INJECTOR"
        vol = {k: float(g[k].fillna(0).sum()) for k in ("oil", "water", "gas", "water_inj", "gas_inj")}
        bhp = float(g["bhp"].replace(0, np.nan).mean()) if g["bhp"].notna().any() else None
        thp = float(g["thp"].replace(0, np.nan).mean()) if g["thp"].notna().any() else None
        bhp = bhp if bhp and not math.isnan(bhp) else None
        thp = thp if thp and not math.isnan(thp) else None
        if inj:
            v = vol["water_inj"] if wells[w]["preferred_phase"] == "WATER" else vol["gas_inj"]
            if days <= 0 or v <= 0:
                events.append({"well_name": w, "event_date": start, "mode": "HISTORY", "status": "SHUT"})
            else:
                events.append({"well_name": w, "event_date": start, "mode": "HISTORY", "status": "OPEN", "control": "RATE",
                               "inj_rate": v / days, "bhp": bhp, "thp": thp})
        else:
            if days <= 0 or (vol["oil"] + vol["water"] + vol["gas"]) <= 0:
                events.append({"well_name": w, "event_date": start, "mode": "HISTORY", "status": "SHUT"})
            else:
                ctl = "ORAT" if vol["oil"] > 0 else ("LRAT" if vol["water"] > 0 else "GRAT")
                events.append({"well_name": w, "event_date": start, "mode": "HISTORY", "status": "OPEN", "control": ctl,
                               "oil_rate": vol["oil"] / days, "water_rate": vol["water"] / days,
                               "gas_rate": vol["gas"] / days, "bhp": bhp, "thp": thp})
    return events


def read_inputs(files: dict) -> dict:
    """files: {'wells': path, 'surveys': path, 'perforations': path, 'history': path, 'controls': path,
    'groups': path, 'history_format': 'standard'|'volve'}. Returns rows for the well model and a gap list."""
    gaps = []
    if not files.get("wells"):
        raise ValueError("a wells table is required")
    w = _lower(read_table(files["wells"]))
    need = {"well_name", "type", "phase"}
    if not need <= set(w.columns):
        raise ValueError(f"wells table needs columns {sorted(need)}")
    wells = {}
    for _, r in w.iterrows():
        name = str(r["well_name"]).strip()
        typ, ph = str(r["type"]).upper(), str(r["phase"]).upper()
        if len(name) > 8:
            gaps.append({"severity": "warning", "item": name, "message": f"well name longer than 8 characters: {name}"})
        wells[name] = {"well_name": name, "well_type": typ, "preferred_phase": ph,
                       "group_name": str(r.get("group") or "FIELD") if pd.notna(r.get("group")) else "FIELD",
                       "head_x": float(r["x"]) if "x" in r and pd.notna(r["x"]) else None,
                       "head_y": float(r["y"]) if "y" in r and pd.notna(r["y"]) else None,
                       "head_i": int(r["i"]) if "i" in r and pd.notna(r["i"]) else None,
                       "head_j": int(r["j"]) if "j" in r and pd.notna(r["j"]) else None,
                       "ref_depth": float(r["ref_depth"]) if "ref_depth" in r and pd.notna(r["ref_depth"]) else None}
    heads = {n: (x["head_x"] or 0.0, x["head_y"] or 0.0) for n, x in wells.items()}
    traj = surveys(read_table(files["surveys"]), heads) if files.get("surveys") else []
    if not traj:
        gaps.append({"severity": "required", "item": "surveys", "message": "no deviation surveys: perforations can't be placed"})
    perfs = []
    if files.get("perforations"):
        p = _lower(read_table(files["perforations"]))
        for _, r in p.iterrows():
            perfs.append({"well_name": str(r["well_name"]), "event_date": pd.to_datetime(r["date"]).date().isoformat(),
                          "top_md": float(r["top_md"]), "bottom_md": float(r["bottom_md"]),
                          "state": str(r.get("state") or "OPEN").upper() if pd.notna(r.get("state")) else "OPEN",
                          "skin": float(r["skin"]) if "skin" in r and pd.notna(r["skin"]) else 0.0,
                          "diameter": float(r["diameter"]) if "diameter" in r and pd.notna(r["diameter"]) else None})
    else:
        gaps.append({"severity": "required", "item": "perforations", "message": "no perforation records"})
    events = []
    if files.get("history"):
        hist = volve_history(files["history"]) if files.get("history_format") == "volve" else read_table(files["history"])
        events += monthly_history(hist, wells)
        unknown = sorted(set(_lower(hist)["well_name"].astype(str)) - set(wells))
        if unknown:
            gaps.append({"severity": "warning", "item": "history", "message": f"history for wells not in the wells table: {unknown[:5]}"})
    if files.get("controls"):
        c = _lower(read_table(files["controls"]))
        for _, r in c.iterrows():
            w_ = str(r["well_name"])
            inj = wells.get(w_, {}).get("well_type") == "INJECTOR"
            ev = {"well_name": w_, "event_date": pd.to_datetime(r["date"]).date().isoformat(), "mode": "PREDICTION",
                  "status": "OPEN", "control": str(r["control"]).upper(),
                  "bhp": float(r["bhp"]) if "bhp" in r and pd.notna(r["bhp"]) else None}
            rate = float(r["rate"]) if "rate" in r and pd.notna(r["rate"]) else None
            key = "inj_rate" if inj else {"ORAT": "oil_rate", "WRAT": "water_rate", "GRAT": "gas_rate", "LRAT": "liquid_rate"}.get(ev["control"])
            if key:
                ev[key] = rate
            events.append(ev)
    if not events:
        gaps.append({"severity": "recommended", "item": "history/controls", "message": "no history or prediction controls"})
    groups = []
    if files.get("groups"):
        g = _lower(read_table(files["groups"]))
        groups = [{"group_name": str(r["group"]), "parent_group": str(r.get("parent") or "FIELD")} for _, r in g.iterrows()]
    else:
        names = {w["group_name"] for w in wells.values()} - {"FIELD"}
        groups = [{"group_name": n, "parent_group": "FIELD"} for n in sorted(names)]
    return {"wells": list(wells.values()), "well_trajectory": traj, "well_completions": perfs, "well_events": events,
            "well_groups": groups, "gaps": gaps}
