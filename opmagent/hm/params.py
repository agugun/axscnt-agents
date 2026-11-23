"""History-match parameters: what can be changed, where (targets), and how a value is applied to the models.

A parameter is {name, kind, target, low, high, start, scale}. Each kind is a change its owner can make to their own
model, applied here in memory the way the modeler would make it, so a deck rebuilt from the modeler's new version
equals the variant (docs/design/history-match.md, section 10).

  kind          owner  type   target                          applied to
  permx_mult    geo    mult   {zone} | {layers: "a-b"} | {}    PERMX and PERMY (unless "permy": false) in the cells
  permz_mult    geo    mult   {zone} | {layers} | {}           PERMZ in the cells
  poro_mult     geo    mult   {zone} | {layers} | {}           PORO in the cells (pore volume)
  multflt       geo    value  {fault}                          the fault's transmissibility multiplier
  corey_nw, corey_now, corey_ng, corey_nog,
  krw_max, krg_max, sorw, swc
                res    value  {satnum}                         that SATNUM's Corey parameter, SWOF/SGOF rebuilt
  aquifer_volume_mult, aquifer_pi_mult
                res    mult   {aquifer}                        a Fetkovich aquifer's water volume / PI

Contacts and well skins come later (docs: decisions, 3). A `mult` start is 1 (the model as approved); a `value`
start defaults to the model's current value.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path

import numpy as np

from ..core import models
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError

CELL_KINDS = {"permx_mult": ("PERMX",), "permz_mult": ("PERMZ",), "poro_mult": ("PORO",)}
COREY_KINDS = {"corey_nw": "nw", "corey_now": "now", "corey_ng": "ng", "corey_nog": "nog", "krw_max": "krw_max",
               "krg_max": "krg_max", "sorw": "sorw", "swc": "swc"}
AQ_KINDS = {"aquifer_volume_mult": "water_volume", "aquifer_pi_mult": "productivity_index"}
KINDS = {**{k: ("geo", "mult") for k in CELL_KINDS}, "multflt": ("geo", "value"),
         **{k: ("res", "value") for k in COREY_KINDS}, **{k: ("res", "mult") for k in AQ_KINDS}}
LATER = {"contact_depth": "contacts (they change the initial state)", "skin": "well skins",
         "owc": "contacts (they change the initial state)"}
COREY_LIMITS = {"nw": (0.5, 8), "now": (0.5, 8), "ng": (0.5, 8), "nog": (0.5, 8), "krw_max": (1e-3, 1),
                "krg_max": (1e-3, 1), "sorw": (0, 0.6), "swc": (0, 0.6)}


# ---------------------------------------------------------------- the base deck's models
def deck_models(con, deck_id: int) -> dict:
    """The four approved models of a deck, as dicts (get_model), plus the deck row."""
    d = models.get_deck(con, deck_id)
    out = {"deck": d}
    for k in ("fluid", "geo", "res", "well"):
        out[k] = models.get_model(con, k, d[f"{k}_version_id"])
    return out


def _geo_spec(geo: dict) -> dict | None:
    sj = geo["header"].get("spec_json")
    return sj.get("geo_spec") if isinstance(sj, dict) else None


def _res_scal(res: dict):
    sj = res["header"].get("spec_json")
    return (sj.get("res_spec") or {}).get("scal") if isinstance(sj, dict) else None


def zones(geo: dict) -> dict[str, tuple[int, int]]:
    """Zone name -> (first, last) layer, 1-based, for geo versions the geo modeler built."""
    gs = _geo_spec(geo)
    if not gs or not gs.get("surfaces") or not gs.get("layers"):
        return {}
    start = np.concatenate([[0], np.cumsum(gs["layers"])])
    return {z: (int(start[i]) + 1, int(start[i + 1])) for i, z in enumerate(gs["surfaces"][:-1])}


def corey_types(res: dict) -> dict[int, dict]:
    """SATNUM -> Corey parameters (defaults filled in), for the rock types the res spec gives as Corey."""
    from ..res.pipeline import COREY_DEFAULTS
    scal = _res_scal(res)
    if scal is None:
        return {}
    types = scal if isinstance(scal, list) else [dict(scal, satnum=1)]
    out = {}
    for n, t in enumerate(types, start=1):
        if t.get("swof") or t.get("sgof"):
            continue
        out[int(t.get("satnum", n))] = {**COREY_DEFAULTS, **{k: t[k] for k in COREY_DEFAULTS if k in t}}
    return out


# ---------------------------------------------------------------- arrays
def read_array(path: str | Path, n: int) -> np.ndarray:
    """A cell array from an include file: one keyword, values with optional n*v repeats, ended by '/'."""
    text = re.sub(r"--[^\n]*", "", Path(path).read_text())
    body = text.split("\n", 1)[1] if re.match(r"\s*[A-Za-z]", text) else text
    body = body.split("/", 1)[0]
    vals: list[float] = []
    for tok in body.split():
        if "*" in tok:
            c, v = tok.split("*", 1)
            vals.extend([float(v)] * int(c))
        else:
            vals.append(float(tok))
    if len(vals) != n:
        raise ToolError(INVALID_INPUT, f"{path}: {len(vals)} values, expected {n}")
    return np.asarray(vals, dtype=float)


def write_array(path: Path, kw: str, values: np.ndarray) -> Path:
    flat = np.asarray(values, dtype=float).ravel()
    with open(path, "w") as f:
        f.write(f"{kw}\n")
        for i in range(0, len(flat), 8):
            f.write(" " + " ".join(f"{v:.10g}" for v in flat[i:i + 8]) + "\n")
        f.write("/\n")
    return path


def _file_path(con, fid: int) -> str:
    r = con.execute("SELECT path FROM files WHERE id = ?", (fid,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"file {fid} not found")
    return r["path"]


# ---------------------------------------------------------------- targets
def targets(con, deck_id: int) -> dict:
    """What parameters can act on in this deck's models, with their current values."""
    m = deck_models(con, deck_id)
    geo, res = m["geo"], m["res"]
    gh = geo["header"]
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    props = {p["keyword"]: p for p in geo["tables"]["geo_properties"]}

    def layer_means(kw):
        p = props.get(kw)
        if not p:
            return None
        if p["const_value"] is not None:
            return [float(p["const_value"])] * nz
        a = read_array(_file_path(con, p["file_id"]), nx * ny * nz).reshape(nz, ny * nx)
        return [float(x) for x in a.mean(axis=1)]

    lm = {kw: layer_means(kw) for kw in ("PERMX", "PERMZ", "PORO")}
    zs = [{"zone": z, "layers": f"{a}-{b}", **{f"mean_{kw.lower()}": (float(np.mean(v[a - 1:b])) if v else None)
                                              for kw, v in lm.items()}} for z, (a, b) in zones(geo).items()]
    layers = [{"layer": k + 1, **{f"mean_{kw.lower()}": (v[k] if v else None) for kw, v in lm.items()}}
              for k in range(nz)]
    faults = [{"fault": f["fault_name"],
               "multflt": next((x["mult"] for x in geo["tables"]["fault_multipliers"]
                                if x["fault_name"] == f["fault_name"]), 1.0)}
              for f in {f["fault_name"]: f for f in geo["tables"]["faults"]}.values()]
    sat = sorted({r["satnum"] for r in res["tables"].get("swof", [])})
    corey = corey_types(res)
    satnums = [{"satnum": s, "corey": corey.get(s), "tabulated": s not in corey} for s in sat]
    aqs = [{"aquifer": a["aquifer_id"], "type": a["aq_type"], "water_volume": a.get("water_volume"),
            "productivity_index": a.get("productivity_index")} for a in res["tables"].get("aquifers", [])]
    return {"deck_id": deck_id, "grid": [nx, ny, nz], "zones": zs, "layers": layers, "faults": faults,
            "satnums": satnums, "aquifers": aqs,
            "kinds": {k: {"owner": o, "type": t} for k, (o, t) in KINDS.items()},
            "later": sorted(set(LATER))}


# ---------------------------------------------------------------- checking a parameter list
def _layer_range(target: dict, geo: dict) -> tuple[int, int]:
    nz = geo["header"]["nz"]
    if target.get("zone"):
        zs = zones(geo)
        if target["zone"] not in zs:
            raise ToolError(INVALID_INPUT, f"unknown zone {target['zone']}; the zones are {sorted(zs) or 'none (give layers)'}")
        return zs[target["zone"]]
    if target.get("layers"):
        a, _, b = str(target["layers"]).partition("-")
        lo, hi = int(a), int(b or a)
        if not 1 <= lo <= hi <= nz:
            raise ToolError(INVALID_INPUT, f"layers {target['layers']} are outside 1-{nz}")
        return lo, hi
    return 1, nz


OPS = ("COPY", "MULTIPLY", "EQUALS", "ADD", "MINVALUE", "MAXVALUE")


def rewritten(spec: dict, keyword: str) -> list[str]:
    """Passthrough keywords of the deck (an imported deck's GRID / EDIT / SCHEDULE text) that set `keyword` after
    the model's array, so a change to the array would not reach Flow: the keyword itself, or COPY / MULTIPLY /
    EQUALS / ADD ... with it as the target. Returns short descriptions."""
    out = []
    for sec in ("GRID", "EDIT", "SCHEDULE"):
        for item in (spec.get("passthrough") or {}).get(sec, []):
            kw = (item.get("keyword") or "").upper()
            raw = item.get("raw_text") or ""
            if kw == keyword:
                out.append(f"{sec} {kw}")
                continue
            if kw in OPS:
                for line in raw.splitlines()[1:]:
                    toks = re.findall(r"'([^']+)'|(\S+)", line)
                    words = [a or b for a, b in toks]
                    if not words or words[0] == "/":
                        continue
                    target = words[1] if kw == "COPY" and len(words) > 1 else words[0]
                    if target.upper() == keyword:
                        out.append(f"{sec} {kw} {' '.join(words[:2])}")
                        break
    return out


def check(params: list[dict], m: dict) -> list[dict]:
    """Normalised parameters, or INVALID_INPUT naming the first problem. m: deck_models()."""
    if not params:
        raise ToolError(INVALID_INPUT, "give at least one parameter")
    geo, res = m["geo"], m["res"]
    spec = (m.get("deck") or {}).get("spec_json") or {}
    out, names = [], set()
    corey = corey_types(res)
    for i, p in enumerate(params, start=1):
        p = dict(p)
        kind = p.get("kind")
        name = p.get("name") or f"{kind}_{i}"
        where = f"parameter {name}"
        if kind in LATER:
            raise ToolError(INVALID_INPUT, f"{where}: {LATER[kind]} come in a later phase")
        if kind not in KINDS:
            raise ToolError(INVALID_INPUT, f"{where}: kind must be one of {', '.join(KINDS)}")
        if name in names:
            raise ToolError(INVALID_INPUT, f"two parameters are named {name}")
        names.add(name)
        owner, typ = KINDS[kind]
        t = dict(p.get("target") or {})
        if kind in CELL_KINDS:
            _layer_range(t, geo)
            cur = None
            kw = CELL_KINDS[kind][0]
            over = rewritten(spec, kw)
            if over:
                raise ToolError(INVALID_INPUT, f"{where}: the deck sets {kw} itself after the model's array "
                                               f"({'; '.join(over[:3])}{' ...' if len(over) > 3 else ''}), so a "
                                               f"multiplier would not reach Flow. Multiply what it is made from, or "
                                               f"bring {kw} into the geo model first")
        elif kind == "multflt":
            over = rewritten(spec, "MULTFLT")
            if over:
                raise ToolError(INVALID_INPUT, f"{where}: the deck sets MULTFLT itself ({'; '.join(over[:3])}), "
                                               "so a change in the geo model would not reach Flow")
            fl = {f["fault_name"] for f in geo["tables"]["faults"]}
            if t.get("fault") not in fl:
                raise ToolError(INVALID_INPUT, f"{where}: unknown fault {t.get('fault')}; the faults are {sorted(fl)}")
            cur = next((x["mult"] for x in geo["tables"]["fault_multipliers"] if x["fault_name"] == t["fault"]), 1.0)
        elif kind in COREY_KINDS:
            s = int(t.get("satnum", 1))
            t["satnum"] = s
            if s not in corey:
                raise ToolError(INVALID_INPUT, f"{where}: SATNUM {s} has no Corey parameters in the res model "
                                               f"(tabulated or missing); Corey SATNUMs: {sorted(corey) or 'none'}")
            cur = corey[s][COREY_KINDS[kind]]
        else:
            aq = {a["aquifer_id"]: a for a in res["tables"].get("aquifers", [])}
            a = aq.get(int(t.get("aquifer", 0)))
            if not a:
                raise ToolError(INVALID_INPUT, f"{where}: unknown aquifer {t.get('aquifer')}; aquifers: {sorted(aq)}")
            if a.get(AQ_KINDS[kind]) is None:
                raise ToolError(INVALID_INPUT, f"{where}: aquifer {a['aquifer_id']} ({a['aq_type']}) has no "
                                               f"{AQ_KINDS[kind]}")
            t["aquifer"] = int(t["aquifer"])
            cur = None
        start = float(p["start"]) if p.get("start") is not None else (1.0 if typ == "mult" else float(cur))
        try:
            lo, hi = float(p["low"]), float(p["high"])
        except (KeyError, TypeError, ValueError):
            raise ToolError(INVALID_INPUT, f"{where}: give low and high") from None
        if not lo < hi:
            raise ToolError(INVALID_INPUT, f"{where}: low {lo:g} must be below high {hi:g}")
        if not lo <= start <= hi:
            raise ToolError(INVALID_INPUT, f"{where}: start {start:g} is outside {lo:g}-{hi:g}")
        scale = p.get("scale") or ("log" if typ == "mult" else "lin")
        if scale not in ("lin", "log"):
            raise ToolError(INVALID_INPUT, f"{where}: scale must be lin or log")
        if scale == "log" and lo <= 0:
            raise ToolError(INVALID_INPUT, f"{where}: a log scale needs a positive range")
        if typ == "mult" and lo < 0:
            raise ToolError(INVALID_INPUT, f"{where}: a multiplier can't be negative")
        if kind == "multflt" and (lo < 0 or hi > 1):
            raise ToolError(INVALID_INPUT, f"{where}: MULTFLT is 0-1")
        if kind in COREY_KINDS:
            a, b = COREY_LIMITS[COREY_KINDS[kind]]
            if lo < a or hi > b:
                raise ToolError(INVALID_INPUT, f"{where}: {COREY_KINDS[kind]} must stay within {a:g}-{b:g}")
        out.append({"name": name, "kind": kind, "owner": owner, "type": typ, "target": t, "low": lo, "high": hi,
                    "start": start, "scale": scale, "current": cur, "active": bool(p.get("active", True))})
    # Swc + Sorw < 1 at every corner of the ranges
    for s, c in corey.items():
        hi = {k: max([c[k]] + [p["high"] for p in out if p["kind"] in ("swc", "sorw") and COREY_KINDS[p["kind"]] == k
                                and p["target"]["satnum"] == s]) for k in ("swc", "sorw")}
        if hi["swc"] + hi["sorw"] >= 0.95:
            raise ToolError(INVALID_INPUT, f"SATNUM {s}: Swc + Sorw can reach {hi['swc'] + hi['sorw']:.2f}; "
                                           "keep it below 0.95 so oil can move")
    return out


def describe(p: dict) -> str:
    t = p["target"]
    where = (f"zone {t['zone']}" if t.get("zone") else f"layers {t['layers']}" if t.get("layers") else
             f"fault {t['fault']}" if t.get("fault") else f"SATNUM {t['satnum']}" if "satnum" in t else
             f"aquifer {t['aquifer']}" if "aquifer" in t else "all cells")
    what = p["kind"].replace("_mult", "").replace("_", " ")
    rng = f"x {p['low']:g}-{p['high']:g}" if p["type"] == "mult" else f"{p['low']:g}-{p['high']:g}"
    return f"{p['name']}: {what} {rng} {p['scale']} in {where} (start {p['start']:g})"


# ---------------------------------------------------------------- applying values
def apply(m: dict, params: list[dict], values: dict[str, float], workdir: Path, con) -> tuple[dict, dict, dict]:
    """(geo, res, file paths) with the values applied; changed arrays are written into workdir.

    geo and res are deep copies; the stored models are never touched. Multipliers on the same keyword compose.
    """
    geo, res = copy.deepcopy(m["geo"]), copy.deepcopy(m["res"])
    gh = geo["header"]
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    n = nx * ny * nz
    props = {p["keyword"]: p for p in geo["tables"]["geo_properties"]}
    arrays: dict[str, np.ndarray] = {}

    def arr(kw):
        if kw not in arrays:
            p = props.get(kw)
            if not p:
                raise ToolError(INVALID_INPUT, f"the geo model has no {kw} to multiply")
            arrays[kw] = (np.full(n, float(p["const_value"])) if p["const_value"] is not None
                          else read_array(_file_path(con, p["file_id"]), n))
        return arrays[kw]

    corey = corey_types(res)
    corey_new = copy.deepcopy(corey)
    for p in params:
        v = float(values.get(p["name"], p["start"]))
        k = p["kind"]
        if p["type"] == "mult" and v == 1.0:      # unchanged: the model stays as stored (a constant stays one)
            continue
        if k in CELL_KINDS:
            lo, hi = _layer_range(p["target"], geo)
            kws = list(CELL_KINDS[k])
            if k == "permx_mult" and p["target"].get("permy", True) and "PERMY" in props:
                kws.append("PERMY")
            for kw in kws:
                a = arr(kw).reshape(nz, ny * nx)
                a[lo - 1:hi] *= v
        elif k == "multflt":
            fm = [x for x in geo["tables"]["fault_multipliers"] if x["fault_name"] != p["target"]["fault"]]
            fm.append({"version_id": gh["id"], "fault_name": p["target"]["fault"], "mult": v})
            geo["tables"]["fault_multipliers"] = fm
        elif k in COREY_KINDS:
            corey_new[p["target"]["satnum"]][COREY_KINDS[k]] = v
        else:
            for a in res["tables"]["aquifers"]:
                if a["aquifer_id"] == p["target"]["aquifer"]:
                    a[AQ_KINDS[k]] = a[AQ_KINDS[k]] * v
    # Corey SATNUMs whose parameters changed: rebuild their SWOF / SGOF as the res modeler does
    changed = [s for s in corey if corey_new[s] != corey[s]]
    if changed:
        from ..res.pipeline import scal_tables
        sw, sg, _ = scal_tables([dict(corey_new[s], satnum=s) for s in changed])
        keep = lambda rows: [r for r in rows if r["satnum"] not in changed]  # noqa: E731
        res["tables"]["swof"] = keep(res["tables"]["swof"]) + sw
        res["tables"]["sgof"] = keep(res["tables"]["sgof"]) + sg
    # file paths of the geo / res arrays: the stored files, with the changed ones replaced by the case's copies
    paths = {}
    for p in list(geo["tables"]["geo_properties"]) + list(res["tables"].get("res_properties") or []):
        if p.get("file_id"):
            paths[p["file_id"]] = _file_path(con, p["file_id"])
    if gh.get("grid_file_id"):
        paths[gh["grid_file_id"]] = _file_path(con, gh["grid_file_id"])
    workdir.mkdir(parents=True, exist_ok=True)
    fake = -1
    for kw, a in arrays.items():
        f = write_array(workdir / f"{kw}.INC", kw, a)
        p = props[kw]
        p["const_value"], p["file_id"] = None, fake
        paths[fake] = str(f)
        fake -= 1
    return geo, res, paths


def values_key(values: dict[str, float]) -> str:
    """A case's identity: its parameter values rounded to 8 significant figures, sorted by name."""
    def r(x):
        return 0.0 if x == 0 else round(x, -int(math.floor(math.log10(abs(x)))) + 7)
    return json.dumps({k: r(float(v)) for k, v in sorted(values.items())}, sort_keys=True)
