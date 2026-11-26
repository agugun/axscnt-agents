"""Res modeler pipeline: spec (+ the geo version's grid) -> draft res model -> validate -> QC report -> submit.

The res model is the dynamic half of the reservoir model, defined on one geo version's grid:

  scal      one rock type as Corey parameters {swc, sorw, sorg, sgc, nw, now, ng, nog, krw_max, krg_max}, or a list
            of rock types [{"satnum": 1, ...Corey...}, {"satnum": 2, "swof": [...], "sgof": [...]}] (measured tables
            as rows {sw, krw, krow, pcow} / {sg, krg, krog, pcog})
  rock      {p_ref, cr} or a list of them per region
  contacts  {datum_depth, datum_pressure, owc_depth, goc_depth} or a list of them per EQLNUM ("eqlnum": n)
  aquifers  optional, a list of analytical aquifers, each Fetkovich or Carter-Tracy, connected to grid faces:
              {"type": "fetkovich", "ct": 1e-4, "water_volume": 5e8, "productivity_index": 50,
               "connections": [{"face": "I-", "layers": "2-3"}]}
              {"type": "carter_tracy", "ct": 1e-4, "permeability": 200, "porosity": 0.25, "inner_radius": 2000,
               "thickness": 30, "angle": 360, "connections": [{"face": "J+", "zone": "Lower", "i": "3-8"}]}
            optional per aquifer: id (default its place in the list), datum_depth (default the OWC of EQLNUM 1, else
            its datum), init_pressure (default: equilibrated with the reservoir), pvtw_table (1); per connection:
            i, j as "a-b" or a number (default: the face's edge of the grid, the whole range along it), layers or
            zone (default all), influx_mult (1). Units are the deck's: METRIC sm3, sm3/day/bar, 1/bar, mD, m.
  regions   optional, per keyword (SATNUM, EQLNUM, FIPNUM, ROCKNUM):
              {"by": "zone", "values": {"Upper": 1, "Lower": 2}}        zones of the geo spec (geo modeler grids)
              {"by": "layer", "values": {"1-3": 1, "4-5": 2}}           layer ranges (any grid)
              {"value": 1}                                              one region everywhere

QC: the res QC notebook (opmagent/notebooks/res_qc.ipynb) on the static view made with this version's regions,
or with [qc] report = "html" a static page (SCAL curves, end points, contacts, rock, regions, aquifers).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..core.errors import INVALID_INPUT, ToolError
from ..core.models import review_state
from ..hello.models import corey_sgof, corey_swof
from ..reports.simple_report import LAYOUT, _list, _page, _table
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap

REGION_KWS = ("SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM")
COREY_DEFAULTS = {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "sgc": 0.05, "nw": 2.0, "now": 2.0, "ng": 2.0, "nog": 2.0,
                  "krw_max": 0.4, "krg_max": 0.8}


def _ok(r):
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def scal_tables(scal) -> tuple[list[dict], list[dict], list[str]]:
    """SWOF and SGOF rows (with satnum) from the spec's SCAL, and what was assumed."""
    types = scal if isinstance(scal, list) else [dict(scal or {}, satnum=1)]
    swof, sgof, notes = [], [], []
    for n, t in enumerate(types, start=1):
        s = int(t.get("satnum", n))
        if t.get("swof") or t.get("sgof"):
            if not (t.get("swof") and t.get("sgof")):
                raise ToolError(INVALID_INPUT, f"SATNUM {s}: give both swof and sgof rows, or Corey parameters")
            swof += [dict(r, satnum=s, pcow=r.get("pcow", 0.0)) for r in t["swof"]]
            sgof += [dict(r, satnum=s, pcog=r.get("pcog", 0.0)) for r in t["sgof"]]
            notes.append(f"SATNUM {s}: measured tables ({t.get('source') or 'source not given'})")
            continue
        given = {k: t[k] for k in COREY_DEFAULTS if k in t}
        c = {**COREY_DEFAULTS, **given}
        swof += [dict(r, satnum=s) for r in corey_swof(c["swc"], c["sorw"], c["nw"], c["now"], c["krw_max"])]
        sgof += [dict(r, satnum=s) for r in corey_sgof(c["swc"], c["sgc"], c["sorg"], c["ng"], c["nog"], c["krg_max"])]
        defaulted = sorted(set(COREY_DEFAULTS) - set(given))
        notes.append(f"SATNUM {s}: Corey {json.dumps(c)}" + (f"; defaults used for {', '.join(defaulted)}" if defaulted
                                                              else "") + (f" ({t['source']})" if t.get("source") else ""))
    return swof, sgof, notes


AQ_TYPES = {"fetkovich": "FETKOVICH", "carter_tracy": "CARTER_TRACY", "carter-tracy": "CARTER_TRACY",
            "ct": "CARTER_TRACY"}
AQ_REQUIRED = {"FETKOVICH": ("ct", "water_volume", "productivity_index"),
               "CARTER_TRACY": ("ct", "permeability", "porosity", "inner_radius", "thickness")}
FACES = ("I-", "I+", "J-", "J+", "K-", "K+")


def _range(v, n: int, what: str) -> tuple[int, int]:
    a, _, b = str(v).partition("-")
    lo, hi = int(a), int(b or a)
    if not 1 <= lo <= hi <= n:
        raise ToolError(INVALID_INPUT, f"{what} {v} is outside 1-{n}")
    return lo, hi


def _zone_layers(zone: str, geo_spec: dict | None, nz: int) -> tuple[int, int]:
    if not geo_spec or not geo_spec.get("surfaces") or not geo_spec.get("layers"):
        raise ToolError(INVALID_INPUT, "an aquifer connection by zone needs a geo version built by the geo modeler")
    zones = geo_spec["surfaces"][:-1]
    if zone not in zones:
        raise ToolError(INVALID_INPUT, f"aquifer connection: unknown zone {zone}; the geo zones are {zones}")
    start = np.concatenate([[0], np.cumsum(geo_spec["layers"])])
    z = zones.index(zone)
    return int(start[z]) + 1, int(start[z + 1])


def aquifer_rows(aquifers, dims: tuple[int, int, int], geo_spec: dict | None,
                 contacts: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """aquifers and aquifer_connections rows from the spec's 'aquifers', and what was assumed."""
    nx, ny, nz = dims
    aqs, conns, notes = [], [], []
    ids = set()
    for n, a in enumerate(aquifers or [], start=1):
        aid = int(a.get("id", n))
        if aid in ids:
            raise ToolError(INVALID_INPUT, f"aquifer {aid} is given twice")
        ids.add(aid)
        typ = AQ_TYPES.get(str(a.get("type", "")).lower())
        if not typ:
            raise ToolError(INVALID_INPUT, f"aquifer {aid}: type must be fetkovich or carter_tracy")
        missing = [k for k in AQ_REQUIRED[typ] if a.get(k) is None]
        if missing:
            raise ToolError(INVALID_INPUT, f"aquifer {aid} ({typ.lower()}): give {', '.join(missing)}")
        said = []
        datum = a.get("datum_depth")
        if datum is None:
            c1 = next((c for c in contacts if c["eqlnum"] == 1), contacts[0])
            datum = c1.get("owc_depth") if c1.get("owc_depth") is not None else c1["datum_depth"]
            said.append(f"datum {datum:g} (the OWC of EQLNUM 1)" if c1.get("owc_depth") is not None
                        else f"datum {datum:g} (the EQLNUM 1 datum)")
        if a.get("init_pressure") is None:
            said.append("initial pressure equilibrated with the reservoir")
        row = {"aquifer_id": aid, "aq_type": typ, "datum_depth": float(datum), "init_pressure": a.get("init_pressure"),
               "total_compressibility": float(a["ct"]), "pvtw_table": int(a.get("pvtw_table", 1))}
        if typ == "FETKOVICH":
            row.update(water_volume=float(a["water_volume"]), productivity_index=float(a["productivity_index"]))
        else:
            row.update(permeability=float(a["permeability"]), porosity=float(a["porosity"]),
                       inner_radius=float(a["inner_radius"]), thickness=float(a["thickness"]),
                       angle_deg=float(a.get("angle", a.get("angle_deg", 360))), influence_table=1)
            if a.get("angle") is None and a.get("angle_deg") is None:
                said.append("angle 360 degrees")
        aqs.append(row)
        if not a.get("connections"):
            raise ToolError(INVALID_INPUT, f"aquifer {aid}: give its connections (a face of the grid, e.g. "
                                           '{"face": "I-"})')
        for c in a["connections"]:
            face = str(c.get("face", "")).upper()
            if face not in FACES:
                raise ToolError(INVALID_INPUT, f"aquifer {aid}: face must be one of {', '.join(FACES)}")
            edge = {"I-": ("i", 1), "I+": ("i", nx), "J-": ("j", 1), "J+": ("j", ny), "K-": ("k", 1), "K+": ("k", nz)}
            ax, at = edge[face]
            i = _range(c["i"], nx, "i") if c.get("i") is not None else ((at, at) if ax == "i" else (1, nx))
            j = _range(c["j"], ny, "j") if c.get("j") is not None else ((at, at) if ax == "j" else (1, ny))
            if c.get("zone"):
                k = _zone_layers(c["zone"], geo_spec, nz)
            elif c.get("layers") is not None or c.get("k") is not None:
                k = _range(c.get("layers", c.get("k")), nz, "layers")
            else:
                k = (at, at) if ax == "k" else (1, nz)
            conns.append({"aquifer_id": aid, "i1": i[0], "i2": i[1], "j1": j[0], "j2": j[1], "k1": k[0], "k2": k[1],
                          "face": face, "influx_mult": float(c.get("influx_mult", 1.0))})
        cells = sum((x["i2"] - x["i1"] + 1) * (x["j2"] - x["j1"] + 1) * (x["k2"] - x["k1"] + 1)
                    for x in conns if x["aquifer_id"] == aid)
        notes.append(f"aquifer {aid}: {typ.lower().replace('_', '-')}, {cells} cell face(s)"
                     + (f"; {', '.join(said)}" if said else "") + (f" ({a['source']})" if a.get("source") else ""))
    return aqs, conns, notes


def _layer_values(rule: dict, nz: int, geo_spec: dict | None, kw: str) -> np.ndarray:
    """One region number per layer (1-based layers)."""
    out = np.zeros(nz, dtype=int)
    if "value" in rule:
        out[:] = int(rule["value"])
        return out
    by, vals = rule.get("by"), rule.get("values") or {}
    if by == "zone":
        if not geo_spec or not geo_spec.get("surfaces") or not geo_spec.get("layers"):
            raise ToolError(INVALID_INPUT, f"{kw} by zone needs a geo version built by the geo modeler (zones)")
        start = np.concatenate([[0], np.cumsum(geo_spec["layers"])])
        zones = geo_spec["surfaces"][:-1]
        unknown = sorted(set(vals) - set(zones))
        if unknown:
            raise ToolError(INVALID_INPUT, f"{kw}: unknown zones {unknown}; the geo zones are {zones}")
        for z, name in enumerate(zones):
            if name in vals:
                out[start[z]:start[z + 1]] = int(vals[name])
    elif by == "layer":
        for rng, v in vals.items():
            a, _, b = str(rng).partition("-")
            lo, hi = int(a), int(b or a)
            if not 1 <= lo <= hi <= nz:
                raise ToolError(INVALID_INPUT, f"{kw}: layer range {rng} is outside 1-{nz}")
            out[lo - 1:hi] = int(v)
    else:
        raise ToolError(INVALID_INPUT, f"{kw}: a region rule needs 'by' (zone or layer) with 'values', or 'value'")
    if (out == 0).any():
        missing = [k + 1 for k in np.flatnonzero(out == 0)]
        raise ToolError(INVALID_INPUT, f"{kw}: layers {missing} get no region")
    return out


def _write_array(path: Path, kw: str, values: np.ndarray) -> Path:
    flat = values.ravel()
    with open(path, "w") as f:
        f.write(f"{kw}\n")
        for i in range(0, len(flat), 16):
            f.write(" " + " ".join(str(int(v)) for v in flat[i:i + 16]) + "\n")
        f.write("/\n")
    return path


def _report(path, name, version, geo, swof, sgof, rock, contacts, regions, assumptions, warnings,
            aquifers=(), aq_conns=()) -> Path:
    figs = []
    for s in sorted({r["satnum"] for r in swof}):
        w, g = [r for r in swof if r["satnum"] == s], [r for r in sgof if r["satnum"] == s]
        fig = make_subplots(rows=1, cols=2, subplot_titles=(f"SWOF (SATNUM {s})", f"SGOF (SATNUM {s})"))
        for key, rows, col, xk in (("krw", w, 1, "sw"), ("krow", w, 1, "sw"), ("krg", g, 2, "sg"), ("krog", g, 2, "sg")):
            fig.add_trace(go.Scatter(x=[r[xk] for r in rows], y=[r[key] for r in rows], name=key, mode="lines+markers"),
                          1, col)
        fig.update_layout(title=f"Relative permeability, SATNUM {s}", **LAYOUT)
        figs.append(fig)
    ends = []
    for s in sorted({r["satnum"] for r in swof}):
        w = [r for r in swof if r["satnum"] == s]
        mobile = [r for r in w if r["krow"] == 0]
        end = mobile[0] if mobile else w[-1]
        ends.append([s, f"{w[0]['sw']:g}", f"{end['sw']:g}", f"{end['krw']:g}", len(w)])
    doc = _page(f"Res model QC: {name} version {version}",
                f"generated {datetime.now():%Y-%m-%d %H:%M} · on geo {geo['model_name']} v{geo['version']} "
                f"({geo['nx']} x {geo['ny']} x {geo['nz']})",
                [("Saturation end points", _table(["SATNUM", "Swc", "Sw at krow = 0", "krw there", "SWOF rows"], ends)),
                 ("Contacts and initial pressure", _table(
                     ["EQLNUM", "Datum depth", "Datum pressure", "OWC", "GOC"],
                     [[c["eqlnum"], c["datum_depth"], c["datum_pressure"], c.get("owc_depth"), c.get("goc_depth")]
                      for c in contacts])),
                 ("Rock compressibility", _table(["Region", "P ref", "cr"], [[r["region"], r["p_ref"], r["cr"]] for r in rock])),
                 ("Regions", _table(["Keyword", "Rule", "Regions"], [[k, json.dumps(v["rule"]), v["n"]] for k, v in regions.items()])
                  if regions else "<p>One region of each kind (no region arrays).</p>"),
                 *([("Analytical aquifers", _table(
                     ["Id", "Type", "Datum", "P init", "ct", "Volume / k", "PI / phi, r, h, angle", "Connections"],
                     [[a["aquifer_id"], a["aq_type"].lower().replace("_", "-"), a["datum_depth"],
                       a["init_pressure"] if a["init_pressure"] is not None else "equilibrated",
                       a["total_compressibility"],
                       a.get("water_volume") if a["aq_type"] == "FETKOVICH" else a.get("permeability"),
                       a.get("productivity_index") if a["aq_type"] == "FETKOVICH" else
                       f"{a['porosity']:g}, {a['inner_radius']:g}, {a['thickness']:g}, {a['angle_deg']:g}",
                       "; ".join(f"{c['face']} i{c['i1']}-{c['i2']} j{c['j1']}-{c['j2']} k{c['k1']}-{c['k2']}"
                                 for c in aq_conns if c["aquifer_id"] == a["aquifer_id"])] for a in aquifers]))]
                   if aquifers else []),
                 ("Assumptions", _list(assumptions)), ("Validator warnings", _list(x["message"] for x in warnings))],
                figs, True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(doc)
    return Path(path)


def resolve_spec(spec: dict, geo_header: dict) -> dict:
    """The res spec as the build applies it on a geo version: SWOF / SGOF rows, contacts per EQLNUM, rock per
    region, aquifer rows, one region number per layer per keyword, and the assumptions. Raises on anything the build
    would refuse. Shared by the build, review proposals and what-if previews."""
    if not spec.get("contacts"):
        raise ToolError(INVALID_INPUT, "the res spec needs contacts (datum_depth, datum_pressure, owc_depth, goc_depth)")
    gh = geo_header
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    geo_spec = (gh.get("spec_json") or {}).get("geo_spec") if isinstance(gh.get("spec_json"), dict) else None
    swof, sgof, notes = scal_tables(spec.get("scal"))
    contacts = spec["contacts"] if isinstance(spec["contacts"], list) else [dict(spec["contacts"], eqlnum=1)]
    contacts = [dict(c, eqlnum=int(c.get("eqlnum", n))) for n, c in enumerate(contacts, start=1)]
    for c in contacts:
        if c.get("datum_depth") is None or c.get("datum_pressure") is None:
            raise ToolError(INVALID_INPUT, f"contacts for EQLNUM {c['eqlnum']} need datum_depth and datum_pressure")
    rk = spec.get("rock") or {"p_ref": contacts[0]["datum_pressure"], "cr": 4.5e-5}
    rock = [dict(r, region=int(r.get("region", n))) for n, r in enumerate(rk if isinstance(rk, list) else [rk], start=1)]
    aqs, aq_conns, aq_notes = aquifer_rows(spec.get("aquifers"), (nx, ny, nz), geo_spec, contacts)
    assumptions = notes + aq_notes + [
        "rock: " + ("; ".join(f"region {r['region']} p_ref {r['p_ref']:g}, cr {r['cr']:g}" for r in rock))
        + ("" if spec.get("rock") else " (default: datum pressure, 4.5e-5 1/bar)"),
        "contacts: " + "; ".join(f"EQLNUM {c['eqlnum']} datum {c['datum_depth']:g} m at {c['datum_pressure']:g} bar, "
                                  f"OWC {c.get('owc_depth')}, GOC {c.get('goc_depth')}" for c in contacts),
    ]
    layers = {}
    for kw, rule in (spec.get("regions") or {}).items():
        kw = kw.upper()
        if kw not in REGION_KWS:
            raise ToolError(INVALID_INPUT, f"regions: {kw} is not one of {REGION_KWS}")
        layers[kw] = (rule, _layer_values(rule, nz, geo_spec, kw))
        assumptions.append(f"{kw}: {json.dumps(rule)}")
    return {"swof": swof, "sgof": sgof, "contacts": contacts, "rock": rock, "aquifers": aqs, "aquifer_connections": aq_conns,
            "layers": layers, "assumptions": assumptions, "dims": (nx, ny, nz)}


def build_res_model(cfg, *, spec: dict, name: str, geo_version_id: int, description: str | None = None,
                    origin: str = "built", source_file_ids: list[int] | None = None,
                    data_source: dict | None = None, extra_assumptions: list[str] | None = None,
                    proposal_id: int | None = None) -> dict:
    """Build a res version on geo_version_id from the spec (see the module docstring) and submit it.
    proposal_id: the accepted review proposal this build applies (res/proposal_build.py), recorded in the spec."""
    if not spec.get("contacts"):
        raise ToolError(INVALID_INPUT, "the res spec needs contacts (datum_depth, datum_pressure, owc_depth, goc_depth)")
    svc = DataService("res", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    geo = svc.get_model(kind="geo", version_id=geo_version_id)
    if not geo.get("ok", True) or "header" not in geo:
        raise ToolError(INVALID_INPUT, f"geo version {geo_version_id} not found")
    gh = geo["header"]
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    r = resolve_spec(spec, gh)
    swof, sgof, contacts, rock = r["swof"], r["sgof"], r["contacts"], r["rock"]
    aqs, aq_conns = r["aquifers"], r["aquifer_connections"]
    assumptions = r["assumptions"] + list(extra_assumptions or [])
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    existing = [m["version"] for m in svc.list_models(kind="res")["models"] if m["model_name"] == name]
    out = cfg.file_store / "models" / "res" / f"{slug}_v{(max(existing) if existing else 0) + 1}"
    out.mkdir(parents=True, exist_ok=True)
    regions, props = {}, []
    for kw, (rule, per_layer) in r["layers"].items():
        if len(set(per_layer)) == 1:
            props.append({"keyword": kw, "const_value": float(per_layer[0])})
        else:
            arr = np.broadcast_to(per_layer[:, None, None], (nz, ny, nx))
            p = _write_array(out / f"{kw}.INC", kw, arr)
            props.append({"keyword": kw, "path": str(p)})
        regions[kw] = {"rule": rule, "n": int(per_layer.max())}

    src = list(source_file_ids or [])
    v = D("create_version", kind="res", model_name=name, origin=origin, source_file_ids=src, header={
        "unit_system": gh["unit_system"], "geo_version_id": geo_version_id, "user_description": description,
        "assumptions_json": assumptions,
        "spec_json": {"res_spec": spec, **({"data_source": data_source} if data_source else {}),
                      **({"proposal_id": proposal_id} if proposal_id else {})}})
    vid = v["version_id"]
    rows = []
    for p in props:
        if "path" in p:
            rows.append({"keyword": p["keyword"], "file_id": D("register_file", path=p["path"], kind="property")["file_id"]})
        else:
            rows.append(p)
    if rows:
        D("write_tables", kind="res", version_id=vid, table="res_properties", rows=rows)
    D("write_tables", kind="res", version_id=vid, table="swof", rows=swof)
    D("write_tables", kind="res", version_id=vid, table="sgof", rows=sgof)
    D("write_tables", kind="res", version_id=vid, table="rock", rows=[{k: r[k] for k in ("region", "p_ref", "cr")} for r in rock])
    D("write_tables", kind="res", version_id=vid, table="contacts", rows=[
        {k: c.get(k) for k in ("eqlnum", "datum_depth", "datum_pressure", "owc_depth", "goc_depth")} for c in contacts])
    if aqs:
        D("write_tables", kind="res", version_id=vid, table="aquifers", rows=aqs)
        D("write_tables", kind="res", version_id=vid, table="aquifer_connections", rows=aq_conns)
    val = D("run_validator", kind="res", version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}
    # the static view first: Flow's own view of this version's regions on the grid, which the QC notebook reads
    from ..geo.static_view import after_res_build
    sv = after_res_build(cfg, svc.con, vid)
    sv_say = sv.pop("static_view_say")
    qc_extra, findings_say = {}, ""
    if cfg.qc_report == "notebook":
        from ..notebooks import runner
        folder = runner.qc_dir(cfg.file_store, name, v["version"], kind="res")
        egrid = sv.get("static_view")
        runner.write_payload(folder, {
            "model_name": name, "version": v["version"], "version_id": vid, "db_path": str(cfg.database),
            "spec": spec, "assumptions": assumptions, "validator_warnings": val["warnings"],
            "regions": {k: x["rule"] for k, x in regions.items()},
            "static_view": {"egrid": egrid, "init": str(Path(egrid).with_suffix(".INIT")) if egrid else None,
                            "error": sv.get("static_view_error")}})
        nb = runner.run_qc("res", folder, cfg.database, vid, timeout=cfg.qc_timeout_s)
        if not nb["ok"]:
            return {"ok": False, "status": "draft", "version_id": vid, "qc_notebook": nb["notebook"],
                    "error": nb["error"], "spoken_summary": f"The QC notebook failed ({nb['error']}). The draft is "
                    f"kept; open {nb['notebook']} to see the failing cell."}
        rep = Path(nb["html"])
        D("register_file", path=nb["notebook"], kind="qc_report", note=f"executed QC notebook, res version {vid}")
        D("register_file", path=nb["payload"], kind="qc_report", note=f"QC payload, res version {vid}")
        s_ = nb["summary"]
        qc_extra = {"qc_notebook": nb["notebook"], "qc_findings": s_["counts"], "qc_overall": s_["overall"],
                    "qc_notebook_sha256": nb["notebook_sha256"]}
        if s_["overall"] != "pass":
            findings_say = (f" The QC notebook flags {s_['counts']['fail']} fail and {s_['counts']['warn']} warn "
                            f"finding(s) for the reviewer.")
    else:
        rep = _report(cfg.file_store / "qc" / f"res_{slug}_v{v['version']}.html", name, v["version"],
                      {**gh, "model_name": gh["model_name"]}, swof, sgof, rock, contacts, regions, assumptions,
                      val["warnings"], aqs, aq_conns)
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    nsat = len({r["satnum"] for r in swof})
    sub = D("submit_for_review", kind="res", version_id=vid, qc_report_file_id=rid,
            metrics={"satnum": nsat, "eqlnum": len(contacts), "regions": {k: v["n"] for k, v in regions.items()},
                     "aquifers": len(aqs), **{k: x for k, x in qc_extra.items() if k != "qc_notebook"}})
    c0 = contacts[0]
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"],
            "qc_report": str(rep), **qc_extra, "assumptions": assumptions, **sv,
            "spoken_summary": f"Res model {name} version {v['version']} {review_state(sub)}: {nsat} saturation "
                              f"region(s), {len(contacts)} equilibration region(s), datum {c0['datum_depth']:g} m at "
                              f"{c0['datum_pressure']:g} bar on geo {gh['model_name']} v{gh['version']}"
                              + (f", {len(aqs)} analytical aquifer(s)" if aqs else "") + "." + findings_say + sv_say}
