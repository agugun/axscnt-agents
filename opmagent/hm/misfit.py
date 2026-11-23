"""Misfit between observed and simulated monthly series, per well and quantity.

    term(w, q) = Σ_t r² / N        r = (sim − obs) / σ,   σ = max(rel_q · |obs|, abs_q)
    total      = Σ_w,q weight_q · Σ_t r² / Σ_w,q N

A quantity the well's history control honours (ORAT honours oil, RATE honours injection, ...) is reported but left
out of the total: Flow was given it, so it says nothing about the model. Terms where observed and simulated are
zero throughout (an injector's oil, a dry well's water) carry no information and are dropped.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from .series import RATES

DEFAULT_OBJECTIVE = {
    "weights": {q: 1.0 for q in (*RATES, "bhp")},
    "sigma": {   # relative error and absolute floor, in the deck's units (METRIC: sm3/d, bar)
        "oil": {"rel": 0.10, "abs": 1.0}, "water": {"rel": 0.10, "abs": 1.0}, "gas": {"rel": 0.10, "abs": 100.0},
        "water_inj": {"rel": 0.10, "abs": 1.0}, "gas_inj": {"rel": 0.10, "abs": 100.0},
        "bhp": {"rel": 0.0, "abs": 5.0},
    },
    "period": None,            # [first month, last month], ISO dates; None: all months
    "exclude_wells": [],
    "include_honoured": False,
}
# history control -> quantities Flow is given (not matched)
HONOURS = {"ORAT": {"oil"}, "WRAT": {"water"}, "GRAT": {"gas"}, "LRAT": set(), "RESV": set(),
           "BHP": {"bhp"}, "THP": set(), "RATE": {"water_inj", "gas_inj"}}
BREAKTHROUGH_WCT = 0.05
ZERO = 1e-9


def objective(overrides: dict | None = None) -> dict:
    """The default objective with overrides merged in (sigma and weights per quantity)."""
    o = copy.deepcopy(DEFAULT_OBJECTIVE)
    for k, v in (overrides or {}).items():
        if k in ("weights", "sigma") and isinstance(v, dict):
            for q, x in v.items():
                if k == "sigma":
                    o["sigma"].setdefault(q, {}).update(x)
                else:
                    o["weights"][q] = float(x)
        elif k in o:
            o[k] = v
        else:
            raise ValueError(f"unknown objective setting '{k}' (weights, sigma, period, exclude_wells, include_honoured)")
    return o


def honoured(con, well_version_id: int) -> dict[str, dict]:
    """Per well: the HISTORY controls it is under, the quantities they honour (in any month), and the timeline of
    controls [(date, control or None)], so each month is judged by the control in force then (a well may switch
    between ORAT and RESV)."""
    out: dict[str, dict] = {}
    for r in con.execute("SELECT well_name, event_date, mode, status, control FROM well_events WHERE version_id = ? "
                         "ORDER BY well_name, event_date", (well_version_id,)):
        d = out.setdefault(r["well_name"], {"controls": set(), "honoured": set(), "timeline": []})
        ctl = r["control"] if r["mode"] == "HISTORY" and r["status"] == "OPEN" else None
        d["timeline"].append((r["event_date"], ctl))
        if ctl:
            d["controls"].add(ctl)
            d["honoured"] |= HONOURS.get(ctl, set())
    return {w: {"controls": sorted(d["controls"]), "honoured": sorted(d["honoured"]), "timeline": d["timeline"]}
            for w, d in out.items() if d["controls"]}


def honoured_mask(df: pd.DataFrame, hon: dict) -> np.ndarray:
    """Per row (well, quantity, month): is the quantity given to Flow by the control in force that month? The
    control in force is the last event on or before the 15th of the month."""
    out = np.zeros(len(df), dtype=bool)
    if df.empty or not hon:
        return out
    for i, (w, q, mo) in enumerate(zip(df["well"], df["quantity"], df["month"])):
        h = hon.get(w)
        if not h or q not in h["honoured"]:
            continue
        if "timeline" not in h:          # no timeline given: honoured throughout
            out[i] = True
            continue
        cut = str(mo)[:8] + "15"
        ctl = None
        for d, c in h.get("timeline", []):
            if d <= cut:
                ctl = c
            else:
                break
        out[i] = bool(ctl) and q in HONOURS.get(ctl, set())
    return out


def align(obs: pd.DataFrame, sim: pd.DataFrame, obj: dict) -> pd.DataFrame:
    """Observed and simulated side by side, on the months both have, with σ and the normalised residual."""
    if obs.empty or sim.empty:
        return pd.DataFrame(columns=["well", "quantity", "month", "obs", "sim", "days", "sigma", "r"])
    a = obs.rename(columns={"value": "obs"}).merge(
        sim[["well", "quantity", "month", "value"]].rename(columns={"value": "sim"}),
        on=["well", "quantity", "month"], how="inner")
    if obj.get("period"):
        lo, hi = obj["period"]
        a = a[(a["month"] >= (lo or "")) & (a["month"] <= (hi or "9999"))]
    if obj.get("exclude_wells"):
        a = a[~a["well"].isin(obj["exclude_wells"])]
    a = a.dropna(subset=["obs", "sim"]).copy()
    rel = a["quantity"].map(lambda q: obj["sigma"].get(q, {}).get("rel", 0.1))
    ab = a["quantity"].map(lambda q: obj["sigma"].get(q, {}).get("abs", 1.0))
    a["sigma"] = np.maximum(rel * a["obs"].abs(), ab)
    a["r"] = (a["sim"] - a["obs"]) / a["sigma"]
    return a.sort_values(["well", "quantity", "month"]).reset_index(drop=True)


def _breakthrough(months, water, oil) -> str | None:
    liq = np.asarray(oil) + np.asarray(water)
    with np.errstate(divide="ignore", invalid="ignore"):
        wct = np.where(liq > ZERO, np.asarray(water) / liq, 0.0)
    idx = np.nonzero(wct > BREAKTHROUGH_WCT)[0]
    return months[idx[0]] if len(idx) else None


def compute(obs: pd.DataFrame, sim: pd.DataFrame, obj: dict | None = None,
            honoured_by_well: dict | None = None) -> dict:
    """Misfit per well and quantity, the total, and the numbers an engineer reads more easily."""
    obj = obj or objective()
    hon = honoured_by_well or {}
    a = align(obs, sim, obj)
    a["honoured"] = honoured_mask(a, hon)
    terms = []
    for (w, q), g in a.groupby(["well", "quantity"]):
        if (g["obs"].abs() < ZERO).all() and (g["sim"].abs() < ZERO).all():
            continue
        used = g if obj.get("include_honoured") else g[~g["honoured"]]
        is_hon = used.empty                      # honoured in every month: shown, not counted
        u = g if is_hon else used
        ss = float((u["r"] ** 2).sum())
        t = {"well": w, "quantity": q, "n": int(len(u)), "sum_sq": ss, "misfit": ss / len(u),
             "honoured": is_hon, "honoured_months": int(g["honoured"].sum()), "in_total": not is_hon,
             "weight": float(obj["weights"].get(q, 1.0))}
        if q in RATES:
            co, cs = float((g["obs"] * g["days"]).sum()), float((g["sim"] * g["days"]).sum())
            t.update(cum_obs=co, cum_sim=cs, cum_error_pct=(100 * (cs - co) / co) if abs(co) > ZERO else None)
        else:
            last = g.iloc[-1]
            t.update(last_month=last["month"], last_obs=float(last["obs"]), last_sim=float(last["sim"]),
                     last_error=float(last["sim"] - last["obs"]))
        terms.append(t)
    tf = pd.DataFrame(terms)
    if tf.empty:
        return {"total": None, "n": 0, "terms": [], "wells": [], "aligned": a}
    used = tf[tf["in_total"]]
    n = int(used["n"].sum())
    total = float((used["weight"] * used["sum_sq"]).sum() / n) if n else None
    wells = []
    for w, g in tf.groupby("well"):
        u = g[g["in_total"]]
        wn = int(u["n"].sum())
        d = {"well": w, "misfit": float((u["weight"] * u["sum_sq"]).sum() / wn) if wn else None, "n": wn,
             "controls": hon.get(w, {}).get("controls", []), "honoured": hon.get(w, {}).get("honoured", [])}
        aw = a[a["well"] == w]
        o = aw[aw["quantity"] == "oil"].set_index("month")
        wa = aw[aw["quantity"] == "water"].set_index("month")
        months = sorted(set(o.index) & set(wa.index))
        if months:
            d["breakthrough_obs"] = _breakthrough(months, wa.loc[months, "obs"], o.loc[months, "obs"])
            d["breakthrough_sim"] = _breakthrough(months, wa.loc[months, "sim"], o.loc[months, "sim"])
        wells.append(d)
    wells.sort(key=lambda d: -(d["misfit"] or 0))
    return {"total": total, "n": n, "terms": sorted(terms, key=lambda t: -t["misfit"]), "wells": wells, "aligned": a}


def describe(result: dict, worst: int = 5) -> list[str]:
    """The worst terms in words, for the spoken summary and the report."""
    out = []
    for t in [t for t in result["terms"] if t["in_total"]][:worst]:
        if t["misfit"] < 1:
            break
        if "cum_error_pct" in t and t["cum_error_pct"] is not None:
            how = f"cumulative {t['quantity'].replace('_', ' ')} {t['cum_error_pct']:+.0f} %"
        elif "last_error" in t:
            how = f"BHP {t['last_error']:+.0f} at the last measurement"
        else:
            how = t["quantity"]
        out.append(f"{t['well']}: {how} (misfit {t['misfit']:.1f})")
    for w in result["wells"]:
        bo, bs = w.get("breakthrough_obs"), w.get("breakthrough_sim")
        if bo and bs and bo != bs:
            months = (int(bs[:4]) - int(bo[:4])) * 12 + int(bs[5:7]) - int(bo[5:7])
            out.append(f"water in {w['well']} arrives {abs(months)} month(s) {'late' if months > 0 else 'early'}")
        elif bo and not bs:
            out.append(f"water in {w['well']} never arrives in the simulation (observed {bo})")
        elif bs and not bo:
            out.append(f"water in {w['well']} arrives in the simulation ({bs}) but not in the history")
    return out
